//! Wire protocol: policy JSON in, report JSON out. No secrets ever ride this channel.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;

pub const MAX_OBSERVED_CHANGES: usize = 256;

/// Policy for one invocation, from the core's neutral backend builder.
#[derive(Debug, Deserialize)]
pub struct Policy {
    pub mode: String,
    pub network: bool,
    pub workspace: String,
    pub writable_roots: Vec<String>,
    pub protected_paths: Vec<String>,
    pub scratch: String,
}

#[derive(Debug, Deserialize)]
pub struct ExecRequest {
    pub policy: Policy,
    pub argv: Vec<String>,
    pub cwd: String,
    pub env: HashMap<String, String>,
    pub stdin_b64: Option<String>,
    pub effects_path: Option<String>,
}


#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ObservedChange {
    pub path: String,
    pub operation: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub old_path: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub content_hash: Option<String>,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub scratch: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ObservedDenial {
    pub operation: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub path: Option<String>,
    pub reason: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NetworkObservation {
    pub allowed: bool,
    pub detail: String,
    #[serde(default)]
    pub attempts: Vec<String>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ObservedEffects {
    #[serde(default)]
    pub changes: Vec<ObservedChange>,
    #[serde(default)]
    pub denials: Vec<ObservedDenial>,
    pub network: Option<NetworkObservation>,
    pub backend: String,
    pub helper: String,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub truncated: bool,
}

#[derive(Debug, Serialize)]
pub struct ExecReport {
    pub exit_code: Option<i32>,
    pub effects: ObservedEffects,
}

pub fn helper_identity() -> String {
    format!(
        "voidcode-sandbox-helper {} (mxc-sdk {})",
        env!("CARGO_PKG_VERSION"),
        mxc_sdk_version()
    )
}

fn mxc_sdk_version() -> &'static str {
    // Pinned via Cargo.lock; surfaced so reports name their engine honestly.
    "1.0.1"
}

pub fn network_observation(allowed: bool) -> NetworkObservation {
    if allowed {
        NetworkObservation {
            allowed: true,
            detail: "unfiltered direct egress (no isolation claimed)".to_string(),
            attempts: vec![],
        }
    } else {
        NetworkObservation {
            allowed: false,
            detail: "private net namespace, no route".to_string(),
            attempts: vec![],
        }
    }
}
