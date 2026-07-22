<?php
/**
 * download.php?job_id=...
 * Streams the finished XLSX. Only serves files for jobs that finished
 * successfully (completed or completed_with_warnings).
 */

require_once __DIR__ . '/config.php';

$job_id = $_GET['job_id'] ?? '';
if (!preg_match('/^[a-f0-9]{32}$/', $job_id)) {
    http_response_code(400);
    exit('Invalid job id');
}

$status_file = JOBS_DIR . '/' . $job_id . '.json';
if (!file_exists($status_file)) {
    http_response_code(404);
    exit('Unknown job');
}

$data = json_decode(file_get_contents($status_file), true);
$ok_states = ['completed', 'completed_with_warnings'];
if (!$data || !in_array($data['status'] ?? '', $ok_states, true)) {
    http_response_code(409);
    exit('File is not ready yet.');
}

// Optional ownership check — uncomment and adapt if jobs are tied to
// logged-in users:
//
// $conn = db_connect();
// $stmt = $conn->prepare('SELECT user_id FROM bank_statement_jobs WHERE job_uid = ?');
// $stmt->bind_param('s', $job_id);
// $stmt->execute();
// $row = $stmt->get_result()->fetch_assoc();
// if (!$row || (int)$row['user_id'] !== (int)($_SESSION['user_id'] ?? 0)) {
//     http_response_code(403);
//     exit('Not authorized.');
// }

$output_path = OUTPUT_DIR . '/' . $job_id . '.xlsx';
$analysis_path = OUTPUT_DIR . '/' . $job_id . '_analysis.html';

$type = $_GET['type'] ?? 'excel';

if ($type === 'analysis') {
    if (!file_exists($analysis_path)) {
        http_response_code(404);
        exit('Analysis report not found for this job.');
    }
    header('Content-Type: text/html; charset=utf-8');
    header('Cache-Control: no-cache, must-revalidate');
    readfile($analysis_path);
    exit;
}

if (!file_exists($output_path)) {
    http_response_code(404);
    exit('Output file missing.');
}

$download_name = 'Bank_Statement_' . date('Y-m-d') . '.xlsx';

header('Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
header('Content-Disposition: attachment; filename="' . $download_name . '"');
header('Content-Length: ' . filesize($output_path));
header('Cache-Control: no-cache, must-revalidate');
readfile($output_path);
