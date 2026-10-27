//! voidcode-sandbox-helper: probe / exec / supervise.
//!
//! Confinement engine is public mxc-sdk (MIT, crates.io). This binary adds:
//! argv-precise supervision (mxc takes a command string run as `sh -c`) and
//! snapshot-diff effect observation. Registry children only: file tools stay host-side.

mod protocol;
mod snapshot;
mod supervise;

use protocol::{helper_identity, network_observation};
use std::path::{Path, PathBuf};

fn main() {
    let mut args = std::env::args().skip(1);
    let subcommand = args.next().unwrap_or_default();
    let code = match subcommand.as_str() {
        "probe" => cmd_probe(),
        "exec" => cmd_exec(),
        "supervise" => {
            let argv_file = args.next().unwrap_or_default();
            cmd_supervise(&argv_file)
        }
        _ => {
            eprintln!("usage: voidcode-sandbox-helper <probe|exec|supervise> (JSON on stdin)");
            2
        }
    };
    std::process::exit(code);
}

fn read_stdin_json<T: serde::de::DeserializeOwned>(what: &str) -> Result<T, i32> {
    // Framed stdin: 8-byte big-endian length + JSON bytes. The frame lets service-style
    // parents keep the pipe open for later caller bytes; a bare-JSON fallback keeps the CLI
    // usable from a shell. Detection: exactly 8 length bytes followed by a `{` payload.
    use std::io::Read;
    let mut stdin = std::io::stdin();
    let mut head = [0u8; 8];
    if stdin.read_exact(&mut head).is_err() {
        eprintln!("{what}: cannot read stdin");
        return Err(2);
    }
    let mut input: Vec<u8> = Vec::new();
    let len = u64::from_be_bytes(head) as usize;
    if len > 0 && len <= 4 * 1024 * 1024 {
        let mut payload = vec![0u8; len];
        if stdin.read_exact(&mut payload).is_ok() && payload.first() == Some(&b'{') {
            input = payload;
        }
    }
    if input.is_empty() {
        // Not a frame: treat the head bytes as the start of bare JSON piped from a shell.
        input.extend_from_slice(&head);
        let mut rest = Vec::new();
        if stdin.read_to_end(&mut rest).is_err() {
            eprintln!("{what}: cannot read stdin");
            return Err(2);
        }
        input.extend(rest);
    }
    let text = String::from_utf8(input).map_err(|e| {
        eprintln!("{what}: stdin is not UTF-8: {e}");
        2
    })?;
    serde_json::from_str(&text).map_err(|e| {
        eprintln!("{what}: invalid JSON: {e}");
        2
    })
}

fn emit_json(value: &impl serde::Serialize) {
    println!("{}", serde_json::to_string(value).unwrap_or_else(|_| "{}".to_string()));
}

fn cmd_probe() -> i32 {
    let backends: Vec<serde_json::Value> = mxc_sdk::v1::available_backends()
        .iter()
        .map(|b| {
            serde_json::json!({
                "backend": b.backend,
                "tier": b.tier,
                "warnings": b.warnings,
            })
        })
        .collect();
    let native = if cfg!(target_os = "linux") {
        "bubblewrap"
    } else if cfg!(target_os = "macos") {
        "seatbelt"
    } else if cfg!(target_os = "windows") {
        "processcontainer"
    } else {
        "unsupported"
    };
    let usable = backends.iter().any(|b| b["backend"] == native);
    emit_json(&serde_json::json!({
        "helper": helper_identity(),
        "native_backend": native,
        "usable": usable,
        "backends": backends,
    }));
    if usable { 0 } else { 1 }
}

fn cmd_exec() -> i32 {
    let request: protocol::ExecRequest = match read_stdin_json("exec") {
        Ok(r) => r,
        Err(code) => return code,
    };
    if request.argv.is_empty() {
        eprintln!("exec: argv must not be empty");
        return 2;
    }
    if request.policy.mode == "danger-full-access" {
        // Unconfined: the supervisor writes effects to the EXPLICIT effects_path (a
        // parent-owned path); the exit code is the process exit code. No report.json.
        return supervise::run_unconfined(&request);
    }
    exec_confined(&request)
}

fn exec_confined(request: &protocol::ExecRequest) -> i32 {
    use mxc_sdk::v1::{
        Containment, ContainerRequest, FilesystemPolicy, NetworkAction, NetworkEgressPolicy, NetworkIngressPolicy,
        NetworkPolicy,
    };
    let policy = &request.policy;
    // Stage the argv file + snapshot manifest OUTSIDE the sandbox-visible tmpfs: the
    // supervisor needs its argv after confinement starts, so it rides the command tail.
    let helper_exe = match std::env::current_exe() {
        Ok(p) => p,
        Err(e) => {
            eprintln!("exec: cannot locate helper binary: {e}");
            return 1;
        }
    };
    let stage = std::env::temp_dir().join(format!("voidcode-sandbox-{}", std::process::id()));
    if std::fs::create_dir_all(&stage).is_err() {
        eprintln!("exec: cannot stage argv");
        return 1;
    }
    let argv_path = stage.join("argv.json");
    // The report channel: a file, never stdout. The child's stdout/stderr stay byte-clean;
    // the exit code + effects report ride this sidecar the parent reads after wait.
    let report_path = stage.join("report.json");
    let argv_payload = serde_json::json!({
        "argv": request.argv,
        "stdin_b64": request.stdin_b64,
        "workspace": policy.workspace,
        "writable_roots": policy.writable_roots,
        "scratch": policy.scratch,
        "network": policy.network,
        "mode": policy.mode,
        "effects_path": stage.join("effects.json").to_string_lossy(),
    });
    if std::fs::write(&argv_path, serde_json::to_string(&argv_payload).unwrap_or_default()).is_err() {
        eprintln!("exec: cannot write argv stage");
        return 1;
    }
    // The supervisor binary must be visible inside the sandbox: bind its own dir read-only.
    let helper_dir = helper_exe.parent().map(Path::to_path_buf).unwrap_or_else(|| PathBuf::from("."));
    let helper_dir_str = helper_dir.to_string_lossy().into_owned();
    let stage_str = stage.to_string_lossy().into_owned();

    let mut readwrite = vec![policy.workspace.clone(), policy.scratch.clone(), stage_str.clone()];
    readwrite.extend(policy.writable_roots.iter().cloned());
    let mut readonly = vec![helper_dir_str];
    // Denied: protected controller/private paths.
    let denied = policy.protected_paths.clone();
    // Read-only mode: nothing writable except scratch/stage (workspace readable).
    if policy.mode == "read-only" {
        readonly.push(policy.workspace.clone());
        readonly.extend(policy.writable_roots.iter().cloned());
        readwrite = vec![policy.scratch.clone(), stage_str.clone()];
    }
    let filesystem = FilesystemPolicy {
        readwrite_paths: readwrite,
        readonly_paths: readonly,
        denied_paths: denied,
        clear_policy_on_exit: None,
    };
    let network = if policy.network {
        // Explicit unfiltered egress: mxc FirewallEnforced needs slirp; without it the
        // backend refuses loudly — which is the honest answer (no silent filtered claim).
        NetworkPolicy {
            egress: Some(NetworkEgressPolicy { default: Some(NetworkAction::Allow), allow: None, deny: None }),
            ingress: Some(NetworkIngressPolicy { default: Some(NetworkAction::Deny), host_loopback: Some(NetworkAction::Deny) }),
            runtime_config: None,
        }
    } else {
        NetworkPolicy {
            egress: Some(NetworkEgressPolicy { default: Some(NetworkAction::Deny), allow: None, deny: None }),
            ingress: Some(NetworkIngressPolicy { default: Some(NetworkAction::Deny), host_loopback: Some(NetworkAction::Deny) }),
            runtime_config: None,
        }
    };
    // Command tail: the supervisor re-execs the exact argv (no shell re-parse of OUR argv;
    // the outer `sh -c` only carries a fixed, quoted helper path + argv-file path).
    let command = format!(
        "{} supervise {}",
        shell_quote(&helper_exe.to_string_lossy()),
        shell_quote(&argv_path.to_string_lossy())
    );
    let mut req = ContainerRequest::new(command);
    req.filesystem = Some(filesystem);
    req.network = Some(network);
    req.containment = Containment::Process;
    req.working_directory = Some(request.cwd.clone());
    let mut env: Vec<(String, String)> = request.env.iter().map(|(k, v)| (k.clone(), v.clone())).collect();
    env.sort_by(|a, b| a.0.cmp(&b.0));
    req.environment = Some(env);
    req.inherit_default_environment = Some(true);
    let mut child = match mxc_sdk::v1::spawn(req, Default::default()) {
        Ok(child) => child,
        Err(e) => {
            eprintln!("exec: backend spawn failed: {e}");
            let _ = std::fs::remove_dir_all(&stage);
            return 1;
        }
    };
    // Take the pipes BEFORE wait: mxc drains (discards) untaken streams on wait, so pump
    // them to OUR stdout/stderr first — the child's output must reach the parent.
    let stdout_thread = child.take_stdout().map(|mut out| {
        std::thread::spawn(move || {
            let mut buf = [0u8; 8192];
            let mut sink = std::io::stdout();
            use std::io::{Read, Write};
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
    let stderr_thread = child.take_stderr().map(|mut err| {
        std::thread::spawn(move || {
            let mut buf = [0u8; 8192];
            let mut sink = std::io::stderr();
            use std::io::{Read, Write};
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
    let outcome = child.wait();
    if let Some(t) = stdout_thread {
        let _ = t.join();
    }
    if let Some(t) = stderr_thread {
        let _ = t.join();
    }
    let exit_code = match outcome {
        Ok(mxc_sdk::v1::WaitResult::Exited(code)) => Some(code),
        Ok(mxc_sdk::v1::WaitResult::TimedOut) => None,
        Err(e) => {
            eprintln!("exec: backend wait failed: {e}");
            let _ = std::fs::remove_dir_all(&stage);
            return 1;
        }
    };
    // Report FIRST, cleanup after: the parent reads report.json, not our stdout (which
    // carries only the child's stdout bytes). Keep the stage dir until the parent reaps it:
    // rename it to a stable, parent-known path instead of deleting it here.
    let effects = read_effects_sidecar(&Some(stage.join("effects.json").to_string_lossy().into_owned()), &policy.scratch, policy.network);
    let report = protocol::ExecReport { exit_code, effects };
    let _ = std::fs::write(&report_path, serde_json::to_string(&report).unwrap_or_default());
    // Hand the stage dir to the parent: print ONLY its path on a dedicated fd (stderr is the
    // child's channel too — so print nothing; the parent derives the path from its own pid).
    // Simplest honest channel: keep stage, parent reaps by pid-keyed path. Print nothing.
    // (The parent knows: temp_dir/voidcode-sandbox-<helper-pid>/report.json.)
    0
}

fn read_effects_sidecar(effects_path: &Option<String>, scratch: &str, network: bool) -> protocol::ObservedEffects {
    let network_obs = network_observation(network);
    let backend = if cfg!(target_os = "linux") {
        "bubblewrap"
    } else if cfg!(target_os = "macos") {
        "seatbelt"
    } else if cfg!(target_os = "windows") {
        "processcontainer"
    } else {
        "unknown"
    };
    // Candidate sidecars: explicit effects_path, then the scratch dir's latest report.
    let mut candidates: Vec<PathBuf> = vec![];
    if let Some(p) = effects_path {
        candidates.push(PathBuf::from(p));
    }
    candidates.push(PathBuf::from(scratch).join("effects.json"));
    for candidate in candidates {
        if let Ok(text) = std::fs::read_to_string(&candidate) {
            if let Ok(mut effects) = serde_json::from_str::<protocol::ObservedEffects>(&text) {
                effects.network = Some(network_obs.clone());
                effects.backend = backend.to_string();
                effects.helper = helper_identity();
                return effects;
            }
        }
    }
    protocol::ObservedEffects {
        changes: vec![],
        denials: vec![],
        network: Some(network_obs),
        backend: backend.to_string(),
        helper: helper_identity(),
        truncated: false,
    }
}

fn cmd_supervise(argv_file: &str) -> i32 {
    if argv_file.is_empty() {
        eprintln!("supervise: argv file required");
        return 2;
    }
    supervise::run_confined(argv_file)
}

fn shell_quote(value: &str) -> String {
    if value.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '/' | '.' | '_' | '-' | ':')) {
        value.to_string()
    } else {
        format!("'{}'", value.replace('\'', "'\\''"))
    }
}
