<?php
/**
 * upload.php
 * Accepts a PDF bank statement, saves it, and QUEUES a conversion job
 * (writes a DB row + status file with status="queued"). It does NOT
 * depend on being able to launch a background process from the web
 * request — that's not available on hosts where exec()/shell_exec()
 * are disabled (very common on shared hosting).
 *
 * If exec() *is* available, it also fires the parser immediately as a
 * best-effort fast path. Either way, worker.php — run every minute via
 * cron — is the thing that guarantees the job actually gets processed,
 * typically within a few seconds of upload, worst case within a minute.
 *
 * Hardening note: everything below is wrapped in one try/catch so a
 * misconfigured server (missing PHP extension, bad DB credentials,
 * wrong folder permissions) returns a JSON error you can actually read
 * in the browser/Network tab, instead of a bare 500 with no explanation.
 */

require_once __DIR__ . '/config.php';

if (session_status() === PHP_SESSION_NONE) {
    session_start();
}

header('Content-Type: application/json');
ini_set('display_errors', '0');   // never let a raw PHP error corrupt the JSON body
error_reporting(E_ALL);           // still log everything to the PHP error log

function fail(string $msg, int $code = 400): void
{
    http_response_code($code);
    echo json_encode(['ok' => false, 'error' => $msg]);
    exit;
}

set_exception_handler(function (Throwable $e) {
    error_log('upload.php fatal: ' . $e->getMessage() . ' at ' . $e->getFile() . ':' . $e->getLine());
    http_response_code(500);
    echo json_encode([
        'ok' => false,
        'error' => 'Server error: ' . $e->getMessage(),
        // Remove these two once everything is working — here purely so
        // you can see exactly what broke during setup.
        'debug_file' => $e->getFile(),
        'debug_line' => $e->getLine(),
    ]);
    exit;
});

try {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        fail('POST required', 405);
    }

    if (!isset($_FILES['statement']) || $_FILES['statement']['error'] !== UPLOAD_ERR_OK) {
        $err = $_FILES['statement']['error'] ?? 'no file';
        fail('Upload failed (error code: ' . $err . '). Check php.ini upload_max_filesize / post_max_size.');
    }

    $file = $_FILES['statement'];

    if ($file['size'] > MAX_UPLOAD_BYTES) {
        fail('File too large. Max ' . (MAX_UPLOAD_BYTES / 1024 / 1024) . ' MB.');
    }

    // Verify it's actually a PDF by content, not just filename. Falls
    // back gracefully if the fileinfo extension isn't installed.
    $is_pdf = false;
    if (function_exists('finfo_open')) {
        $finfo = finfo_open(FILEINFO_MIME_TYPE);
        if ($finfo !== false) {
            $mime = finfo_file($finfo, $file['tmp_name']);
            finfo_close($finfo);
            $is_pdf = ($mime === 'application/pdf');
        }
    }
    if (!$is_pdf) {
        // Fallback: PDFs start with the 5 bytes "%PDF-".
        $handle = fopen($file['tmp_name'], 'rb');
        $head = $handle ? fread($handle, 5) : '';
        if ($handle) fclose($handle);
        $is_pdf = ($head === '%PDF-');
    }
    if (!$is_pdf) {
        fail('Only PDF files are accepted.');
    }

    // Optional: require a logged-in user. Wire this up to your existing
    // session/auth system. Left permissive (nullable) so the tool works
    // standalone too.
    $user_id = $_SESSION['user_id'] ?? null;

    $job_uid = generate_job_uid();
    $original_name = basename($file['name']);
    $stored_pdf_path = UPLOAD_DIR . '/' . $job_uid . '.pdf';
    $output_xlsx_path = OUTPUT_DIR . '/' . $job_uid . '.xlsx';
    $output_analysis_path = OUTPUT_DIR . '/' . $job_uid . '_analysis.html';
    $status_file = JOBS_DIR . '/' . $job_uid . '.json';
    $log_file = JOBS_DIR . '/' . $job_uid . '.log';

    foreach ([UPLOAD_DIR, OUTPUT_DIR, JOBS_DIR] as $dir) {
        if (!is_dir($dir) || !is_writable($dir)) {
            fail("Server folder not writable: $dir (check permissions — the web server user needs write access).", 500);
        }
    }

    if (!move_uploaded_file($file['tmp_name'], $stored_pdf_path)) {
        fail('Could not save uploaded file (check folder permissions on ' . UPLOAD_DIR . ').', 500);
    }

    // Seed the status file — this is what makes the job "queued" and
    // pickable by worker.php, regardless of whether the instant
    // background launch below succeeds.
    file_put_contents($status_file, json_encode([
        'status'          => 'queued',
        'progress'        => 0,
        'message'         => 'Queued',
        'job_id'          => $job_uid,
        'input_path'      => $stored_pdf_path,
        'output_path'     => $output_xlsx_path,
        'analysis_path'   => $output_analysis_path,
        'log_path'        => $log_file,
        'queued_at'       => date('c'),
    ]));

    try {
        $conn = db_connect();
        $stmt = $conn->prepare(
            'INSERT INTO bank_statement_jobs
                (job_uid, user_id, original_filename, stored_pdf_path, status)
             VALUES (?, ?, ?, ?, "queued")'
        );
        $stmt->bind_param('siss', $job_uid, $user_id, $original_name, $stored_pdf_path);
        $stmt->execute();
        $stmt->close();
        $conn->close();
    } catch (Throwable $e) {
        // Don't block the conversion just because the DB insert failed —
        // log it and continue; status.php still works off the JSON file,
        // and worker.php will still pick the job up from JOBS_DIR either way.
        error_log('bank_statement_jobs insert failed: ' . $e->getMessage());
    }

    // --- Best-effort instant launch -----------------------------------
    // Only attempted if exec() is actually available. If it's not (the
    // common case on shared hosting), we simply leave the job queued —
    // worker.php (run via cron every minute) will process it shortly.
    // This is not an error condition, so we don't fail() here.
    if (function_exists('exec') && file_exists(PARSER_PATH)) {
        $cmd = sprintf(
            '%s %s --input %s --output %s --analysis-output %s --status-file %s --job-id %s > %s 2>&1 &',
            escapeshellcmd(PYTHON_BIN),
            escapeshellarg(PARSER_PATH),
            escapeshellarg($stored_pdf_path),
            escapeshellarg($output_xlsx_path),
            escapeshellarg($output_analysis_path),
            escapeshellarg($status_file),
            escapeshellarg($job_uid),
            escapeshellarg($log_file)
        );
        @exec($cmd);
    }

    echo json_encode([
        'ok'     => true,
        'job_id' => $job_uid,
        'status' => 'queued',
    ]);

} catch (Throwable $e) {
    error_log('upload.php error: ' . $e->getMessage());
    http_response_code(500);
    echo json_encode([
        'ok' => false,
        'error' => 'Server error: ' . $e->getMessage(),
        'debug_file' => $e->getFile(),
        'debug_line' => $e->getLine(),
    ]);
}
