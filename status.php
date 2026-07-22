<?php
/**
 * status.php?job_id=...
 * Polled by the browser every couple of seconds. Reads the JSON status
 * file the Python parser writes to as it works, so the client sees live
 * progress without the HTTP request itself ever blocking.
 * On first "completed"/"completed_with_warnings"/"failed" it also syncs
 * the result into MySQL for history/reporting.
 */

require_once __DIR__ . '/config.php';

header('Content-Type: application/json');

$job_id = $_GET['job_id'] ?? '';
if (!preg_match('/^[a-f0-9]{32}$/', $job_id)) {
    http_response_code(400);
    echo json_encode(['ok' => false, 'error' => 'Invalid job id']);
    exit;
}

$status_file = JOBS_DIR . '/' . $job_id . '.json';
if (!file_exists($status_file)) {
    http_response_code(404);
    echo json_encode(['ok' => false, 'error' => 'Unknown job']);
    exit;
}

$data = json_decode(file_get_contents($status_file), true);
if ($data === null) {
    // File was mid-write; ask the client to retry shortly.
    echo json_encode(['ok' => true, 'status' => 'processing', 'progress' => 0, 'message' => 'Working...']);
    exit;
}

$terminal_states = ['completed', 'completed_with_warnings', 'failed'];
if (in_array($data['status'] ?? '', $terminal_states, true)) {
    sync_job_to_db($job_id, $data);
}

echo json_encode(array_merge(['ok' => true], $data));

/**
 * Write the final result to MySQL exactly once (guarded by only updating
 * rows still in a non-terminal state, so repeated polls after completion
 * don't hammer the DB).
 */
function sync_job_to_db(string $job_uid, array $data): void
{
    try {
        $conn = db_connect();
        $stmt = $conn->prepare(
            'UPDATE bank_statement_jobs SET
                status = ?, progress = ?, message = ?, bank_name = ?,
                transactions_count = ?, total_withdrawals = ?, total_deposits = ?,
                opening_balance = ?, closing_balance = ?, balance_mismatches = ?,
                error_message = ?, output_xlsx_path = ?, analysis_html_path = ?, audit_flags_count = ?
             WHERE job_uid = ? AND status NOT IN ("completed","completed_with_warnings","failed")'
        );
        $output_path = OUTPUT_DIR . '/' . $job_uid . '.xlsx';
        $analysis_path = OUTPUT_DIR . '/' . $job_uid . '_analysis.html';
        $status = $data['status'] ?? 'failed';
        $progress = $data['progress'] ?? 100;
        $message = $data['message'] ?? null;
        $bank = $data['bank_name'] ?? null;
        $count = $data['transactions_count'] ?? null;
        $tw = $data['total_withdrawals'] ?? null;
        $td = $data['total_deposits'] ?? null;
        $ob = $data['opening_balance'] ?? null;
        $cb = $data['closing_balance'] ?? null;
        $mm = $data['balance_mismatches'] ?? null;
        $err = $data['error'] ?? null;
        $flags = $data['audit_flags_count'] ?? null;

        $stmt->bind_param(
            'sissiddddisssis',
            $status, $progress, $message, $bank,
            $count, $tw, $td, $ob, $cb, $mm, $err, $output_path, $analysis_path, $flags, $job_uid
        );
        $stmt->execute();
        $stmt->close();
        $conn->close();
    } catch (Throwable $e) {
        error_log('sync_job_to_db failed: ' . $e->getMessage());
    }
}
