//! Supervisor: snapshot, run exact argv, flush diff — even under kill/timeout.
//!
//! Runs INSIDE the sandbox (confined path) or as a plain parent (danger mode).
//! Child stdio are plain pipes the supervisor pumps to its own stdout/stderr.

use crate::protocol::{network_observation, ExecRequest, ObservedEffects};
use crate::snapshot::Snapshot;
use std::io::{Read, Write};
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

pub fn run_confined(argv_file: &str) -> i32 {
    let text = match std::fs::read_to_string(argv_file) {
        Ok(t) => t,
        Err(e) => {
            eprintln!("supervise: cannot read argv file: {e}");
            return 2;
        }
    };
    let payload: serde_json::Value = match serde_json::from_str(&text) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("supervise: invalid argv file: {e}");
            return 2;
        }
    };
    let argv: Vec<String> = payload["argv"]
        .as_array()
        .map(|a| a.iter().filter_map(|v| v.as_str().map(str::to_string)).collect())
        .unwrap_or_default();
    if argv.is_empty() {
        eprintln!("supervise: empty argv");
        return 2;
    }
    let stdin_bytes = payload["stdin_b64"].as_str().and_then(|b| {
        use base64::Engine;
        base64::engine::general_purpose::STANDARD.decode(b).ok()
    });
    let workspace = payload["workspace"].as_str().unwrap_or("").to_string();
    let writable_roots: Vec<String> = payload["writable_roots"]
        .as_array()
        .map(|a| a.iter().filter_map(|v| v.as_str().map(str::to_string)).collect())
        .unwrap_or_default();
    let scratch = payload["scratch"].as_str().unwrap_or("").to_string();
    let network = payload["network"].as_bool().unwrap_or(false);
    let effects_path = payload["effects_path"].as_str().map(str::to_string);
    // The supervisor runs confined: relative roots resolve against OUR cwd (the request cwd).
    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from(&workspace));
    let workspace_abs = absolutize(&workspace, &cwd).to_string_lossy().into_owned();
    let writable_abs: Vec<String> =
        writable_roots.iter().map(|r| absolutize(r, &cwd).to_string_lossy().into_owned()).collect();
    let scratch_abs = absolutize(&scratch, &cwd).to_string_lossy().into_owned();
    run_supervised(&argv, stdin_bytes, &workspace_abs, &writable_abs, &scratch_abs, network, effects_path, false)
}

pub fn run_unconfined(request: &ExecRequest) -> i32 {
    use base64::Engine;
    run_supervised(
        &request.argv,
        request
            .stdin_b64
            .as_deref()
            .and_then(|b| base64::engine::general_purpose::STANDARD.decode(b).ok()),
        &request.policy.workspace,
        &request.policy.writable_roots,
        &request.policy.scratch,
        request.policy.network,
        request.effects_path.clone(),
        true,
    )
}

fn absolutize(path: &str, cwd: &PathBuf) -> PathBuf {
    let candidate = PathBuf::from(path);
    if candidate.is_absolute() {
        candidate
    } else {
        cwd.join(candidate)
    }
}

#[allow(clippy::too_many_arguments)]
fn run_supervised(
    argv: &[String],
    stdin_bytes: Option<Vec<u8>>,
    workspace: &str,
    writable_roots: &[String],
    scratch: &str,
    network: bool,
    effects_path: Option<String>,
    unconfined: bool,
) -> i32 {
    let mut roots: Vec<PathBuf> = vec![PathBuf::from(workspace)];
    roots.extend(writable_roots.iter().map(PathBuf::from));
    let scratch_buf = PathBuf::from(scratch);
    let before = Snapshot::capture(&roots, Some(&scratch_buf));

    let dying = Arc::new(AtomicBool::new(false));
    install_flush_on_signal(dying.clone());

    let mut command = std::process::Command::new(&argv[0]);
    command.args(&argv[1..]);
    command.stdin(if stdin_bytes.is_some() { Stdio::piped() } else { Stdio::null() });
    command.stdout(Stdio::piped());
    command.stderr(Stdio::piped());
    // The supervisor's own env is already the confined, explicit env (mxc --clearenv +
    // setenv, or the plan env in danger mode): inherit it untouched.
    let mut child = match command.spawn() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("supervise: cannot spawn {}: {e}", argv[0]);
            finalize(before, &roots, Some(&scratch_buf), network, &effects_path, unconfined);
            return 127;
        }
    };
    if let (Some(mut pipe), Some(bytes)) = (child.stdin.take(), stdin_bytes) {
        let _ = pipe.write_all(&bytes);
        drop(pipe);
    }
    // Pump child stdout/stderr to ours on helper threads; a broken sink never alters the result.
    let stdout_thread = child.stdout.take().map(|mut out| {
        std::thread::spawn(move || {
            let mut buf = [0u8; 8192];
            let mut sink = std::io::stdout();
            loop {
                match out.read(&mut buf) {
                    Ok(0) | Err(_) => break,
                    Ok(n) => {
                        let _ = sink.write_all(&buf[..n]);
                    }
                }
            }
            let _ = sink.flush();
        })
    });
    let stderr_thread = child.stderr.take().map(|mut err| {
        std::thread::spawn(move || {
            let mut buf = [0u8; 8192];
            let mut sink = std::io::stderr();
            loop {
                match err.read(&mut buf) {
                    Ok(0) | Err(_) => break,
                    Ok(n) => {
                        let _ = sink.write_all(&buf[..n]);
                    }
                }
            }
            let _ = sink.flush();
        })
    });
    // Wait for the child; on supervisor-signal, kill the child first, then flush below.
    let status = loop {
        if dying.load(Ordering::SeqCst) {
            let _ = child.kill();
        }
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) => std::thread::sleep(std::time::Duration::from_millis(20)),
            Err(e) => {
                eprintln!("supervise: wait failed: {e}");
                finalize(before, &roots, Some(&scratch_buf), network, &effects_path, unconfined);
                return 1;
            }
        }
    };
    if let Some(t) = stdout_thread {
        let _ = t.join();
    }
    if let Some(t) = stderr_thread {
        let _ = t.join();
    }
    finalize(before, &roots, Some(&scratch_buf), network, &effects_path, unconfined);
    status.code().unwrap_or(1)
}

fn finalize(
    before: Snapshot,
    roots: &[PathBuf],
    scratch: Option<&PathBuf>,
    network: bool,
    effects_path: &Option<String>,
    unconfined: bool,
) {
    let effects = build_effects(before, roots, scratch, network, unconfined);
    let text = serde_json::to_string(&effects).unwrap_or_default();
    // Primary: explicit effects path. Fallback: scratch/effects.json. Best effort either way.
    if let Some(path) = effects_path {
        if std::fs::write(path, &text).is_ok() {
            return;
        }
    }
    if let Some(dir) = scratch {
        let _ = std::fs::create_dir_all(dir);
        let _ = std::fs::write(dir.join("effects.json"), &text);
    }
}

pub fn build_effects(
    before: Snapshot,
    roots: &[PathBuf],
    scratch: Option<&PathBuf>,
    network: bool,
    unconfined: bool,
) -> ObservedEffects {
    let after = Snapshot::capture(roots, scratch.map(|p| p.as_path()));
    let (changes, truncated) = Snapshot::diff(before, after);
    let mut detail = network_observation(network);
    if unconfined {
        detail.detail = format!("{} -- observation best-effort (unconfined supervisor)", detail.detail);
    }
    ObservedEffects {
        changes,
        denials: vec![],
        network: Some(detail),
        backend: String::new(),
        helper: String::new(),
        truncated,
    }
}

fn install_flush_on_signal(dying: Arc<AtomicBool>) {
    #[cfg(unix)]
    {
        for signal in [signal_hook::consts::SIGTERM, signal_hook::consts::SIGINT] {
            let flag = dying.clone();
            // ponytail: a bool flag + poll loop, not an async-safe handler doing I/O.
            unsafe {
                let _ = signal_hook::low_level::register(signal, move || {
                    flag.store(true, Ordering::SeqCst);
                });
            }
        }
    }
    #[cfg(windows)]
    {
        let flag = dying.clone();
        let _ = ctrlc::set_handler(move || {
            flag.store(true, Ordering::SeqCst);
        });
    }
}
