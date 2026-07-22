#!/usr/bin/env php
<?php
/**
 * worker.php
 * Run this on a schedule via cron, e.g. every minute:
 *
 *   * * * * * /usr/bin/php /full/path/to/bank_tool/worker.php >> /full/path/to/bank_tool/jobs/worker.log 2>&1
 *
 * It scans jobs/*.json for anything still "queued", runs the Python
 * parser for each one (via shell_exec, which is invoked here from PHP
 * CLI under cron — most hosts that disable exec()/shell_exec() for the
 * web-facing PHP-FPM/mod_php pool still allow it for CLI/cron, since
 * that's not attacker-reachable the way a web endpoint is), and updates
 * the status file + database as each job finishes.
 *
 * A file lock (flock) stops two overlapping cron ticks from processing
 * the same job twice. The loop keeps picking up queued jobs until none
 * are left or ~50 seconds have passed, so it plays nicely with a
 * once-a-minute cron schedule without jobs piling up.
 *
 * IMPORTANT: if shell_exec()/exec()/proc_open() are ALSO disabled for
 * CLI on your host, PHP genuinely cannot invoke Python at all and this
 * approach won't work — you'd need a true daemon (e.g. a systemd
 * service or supervisor process running parser/parse_statement.py
 * directly, watching the jobs/ folder itself) instead of PHP-driven
 * dispatch. That's a hosting-plan limitation, not a bug in this script.
 */

if (php_sapi_name() !== 'cli') {
    http_response_code(403);
    exit("worker.php must be run from the command line / cron, not the browser.\n");
}

require_once __DIR__ . '/config.php';

$lock_file = JOBS_DIR . '/worker.lock';
$lock_handle = fopen($lock_file, 'c');
if (!$lock_handle || !flock($lock_handle, LOCK_EX | LOCK_NB)) {
    echo "[" . date('c') . "] Another worker instance is already running. Exiting.\n";
    exit(0);
}

$can_run_shell = function_exists('shell_exec') || function_exists('exec') || function_exists('proc_open');
if (!$can_run_shell) {
    echo "[" . date('c') . "] FATAL: shell_exec(), exec(), and proc_open() are all disabled "
        . "for PHP CLI on this host. Cannot invoke the Python parser. "
        . "Ask your host to allow at least one of these for CLI scripts, "
        . "or run parser/parse_statement.py via a separate daemon.\n";
    flock($lock_handle, LOCK_UN);
    fclose($lock_handle);
    exit(1);
}

$start_time = time();
$time_budget_seconds = 50; // leave headroom before the next cron tick fires
$processed = 0;

while (time() - $start_time < $time_budget_seconds) {
    $job = find_next_queued_job();
    if (!$job) {
        break; // nothing left to do
    }
    process_job($job);
    $processed++;
}

echo "[" . date('c') . "] Worker run complete. Jobs processed: $processed\n";

flock($lock_handle, LOCK_UN);
fclose($lock_handle);
exit(0);

// -------------------------------------------------------------------------

/**
 * Finds the oldest queued job among jobs/*.json, and atomically claims
 * it by rewriting its status to "processing" before returning — so a
 * second worker.php invocation (or a slow one still finishing its
 * previous job when the next cron tick starts) won't pick up the same
 * job twice.
 */
function find_next_queued_job(): ?array
{
    $files = glob(JOBS_DIR . '/*.json');
    if (!$files) {
        return null;
    }

    $candidates = [];
    foreach ($files as $f) {
        $data = json_decode(file_get_contents($f), true);
        if (is_array($data) && ($data['status'] ?? '') === 'queued') {
            $candidates[] = ['file' => $f, 'data' => $data];
        }
    }
    if (!$candidates) {
        return null;
    }

    usort($candidates, fn($a, $b) =>
        strcmp($a['data']['queued_at'] ?? '', $b['data']['queued_at'] ?? ''));
    $chosen = $candidates[0];

    // Claim it immediately so a concurrent worker skips it.
    $chosen['data']['status'] = 'processing';
    $chosen['data']['message'] = 'Picked up by worker';
    $chosen['data']['progress'] = 1;
    file_put_contents($chosen['file'], json_encode($chosen['data']));

    return $chosen['data'];
}

function process_job(array $job): void
{
    $job_id = $job['job_id'] ?? null;
    $input = $job['input_path'] ?? null;
    $output = $job['output_path'] ?? null;
    $analysis_output = $job['analysis_path'] ?? ($output ? preg_replace('/\.xlsx$/', '_analysis.html', $output) : null);
    $status_file = JOBS_DIR . '/' . $job_id . '.json';
    $log_file = $job['log_path'] ?? (JOBS_DIR . '/' . $job_id . '.log');

    if (!$job_id || !$input || !$output || !file_exists($input)) {
        file_put_contents($status_file, json_encode([
            'status' => 'failed',
            'progress' => 100,
            'error' => 'Job record was incomplete or the source PDF was missing.',
            'job_id' => $job_id,
        ]));
        return;
    }

    echo "[" . date('c') . "] Processing job $job_id ...\n";

    $cmd = sprintf(
        '%s %s --input %s --output %s --analysis-output %s --status-file %s --job-id %s >> %s 2>&1',
        escapeshellcmd(PYTHON_BIN),
        escapeshellarg(PARSER_PATH),
        escapeshellarg($input),
        escapeshellarg($output),
        escapeshellarg($analysis_output),
        escapeshellarg($status_file),
        escapeshellarg($job_id),
        escapeshellarg($log_file)
    );

    // parse_statement.py itself keeps rewriting $status_file with live
    // progress and the final result — this call runs it to completion
    // synchronously (fine here: we're CLI, no web request is waiting).
    $exit_code = 0;
    if (function_exists('exec')) {
        exec($cmd, $out, $exit_code);
    } elseif (function_exists('shell_exec')) {
        shell_exec($cmd);
    } elseif (function_exists('proc_open')) {
        $proc = proc_open($cmd, [1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes);
        if (is_resource($proc)) {
            fclose($pipes[1]);
            fclose($pipes[2]);
            $exit_code = proc_close($proc);
        }
    }

    // If the python script crashed before writing its own "failed"
    // status, make sure the job doesn't sit stuck as "processing" forever.
    $final = json_decode(@file_get_contents($status_file), true);
    $terminal = ['completed', 'completed_with_warnings', 'failed'];
    if (!is_array($final) || !in_array($final['status'] ?? '', $terminal, true)) {
        file_put_contents($status_file, json_encode([
            'status' => 'failed',
            'progress' => 100,
            'error' => 'Parser exited unexpectedly (exit code ' . $exit_code . '). See ' . $log_file,
            'job_id' => $job_id,
        ]));
    }

    echo "[" . date('c') . "] Job $job_id finished.\n";
}
