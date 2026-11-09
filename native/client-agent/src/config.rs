use std::collections::{HashMap, HashSet};
use std::fs;
use std::net::Ipv4Addr;
use std::path::Path;

use anyhow::{Context, Result, bail};
use serde::Deserialize;
use sha2::{Digest, Sha256};

pub const CONFIG_SCHEMA_VERSION: u32 = 2;
pub const CONTROL_PROTOCOL_VERSION: u32 = 1;
pub const PINGMESH_CONTROL_PORT: u16 = 9910;
pub const TRAFFIC_CONTROL_PORT: u16 = 9911;

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ClientAgentConfig {
    pub schema_version: u32,
    pub topology_id: String,
    pub pingmesh_plan: PlanSettings,
    pub client_plan_sha256: HashMap<String, String>,
    pub clients: Vec<Client>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Client {
    pub name: String,
    pub data_ip: Ipv4Addr,
    pub management_ip: Ipv4Addr,
    pub rack: String,
    pub leaf: String,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlanSettings {
    pub plan_id: String,
    pub source_ports: Vec<u16>,
    pub port_batches: Vec<Vec<usize>>,
    pub destination_batch_size: usize,
    pub cycle_interval_seconds: u64,
    pub coverage_epoch_cycles: usize,
    pub coverage_epoch_seconds: u64,
    pub complete_window_seconds: u64,
    pub df_payload_size: usize,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, Hash, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum ProbeKind {
    Rtt,
    Df,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProbeFrame {
    pub destination_batch_index: usize,
    pub port_batch_index: usize,
    pub target_indices: Vec<usize>,
    pub probes: Vec<(ProbeKind, usize, usize)>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ClientProbePlan {
    pub schema_version: u32,
    pub plan_id: String,
    pub name: String,
    pub targets: Vec<String>,
    pub startup_delay_ms: u64,
    pub frames: Vec<ProbeFrame>,
}

impl ClientAgentConfig {
    pub fn load(path: &Path) -> Result<Self> {
        let bytes = fs::read(path).with_context(|| format!("read {}", path.display()))?;
        let config: Self = serde_json::from_slice(&bytes)
            .context("parse current client-agent config; regenerate old artifacts")?;
        config.validate()?;
        Ok(config)
    }

    pub fn local_client(&self) -> Result<Client> {
        let hostname = std::env::var("HOSTNAME").context("HOSTNAME is required")?;
        self.clients
            .iter()
            .find(|client| client.name == hostname)
            .cloned()
            .with_context(|| format!("HOSTNAME {hostname:?} is not present in client-agent config"))
    }

    pub fn load_client_plan(&self, config_path: &Path, local: &Client) -> Result<ClientProbePlan> {
        let path = config_path
            .parent()
            .context("config directory is required")?
            .join("pinglists")
            .join(format!("{}.json", local.name));
        let bytes = fs::read(&path).with_context(|| format!("read {}", path.display()))?;
        let digest = format!("{:x}", Sha256::digest(&bytes));
        if self.client_plan_sha256.get(&local.name) != Some(&digest) {
            bail!("client plan digest mismatch; regenerate the topology");
        }
        let plan: ClientProbePlan = serde_json::from_slice(&bytes).context("parse client plan")?;
        plan.validate(self, local)?;
        Ok(plan)
    }

    fn validate(&self) -> Result<()> {
        if self.schema_version != CONFIG_SCHEMA_VERSION {
            bail!(
                "unsupported client-agent schema; expected {}; regenerate the topology",
                CONFIG_SCHEMA_VERSION
            );
        }
        let names: HashSet<_> = self.clients.iter().map(|client| &client.name).collect();
        let settings = &self.pingmesh_plan;
        if self.clients.len() < 2
            || names.len() != self.clients.len()
            || self
                .clients
                .iter()
                .any(|client| client.name.contains('/') || client.name == "..")
            || self.client_plan_sha256.keys().collect::<HashSet<_>>() != names
        {
            bail!("invalid client roster or client plan digests");
        }
        if settings.plan_id.len() != 64
            || settings.source_ports.is_empty()
            || settings.source_ports.contains(&33434)
            || settings.source_ports.iter().collect::<HashSet<_>>().len()
                != settings.source_ports.len()
            || settings.cycle_interval_seconds == 0
            || settings.coverage_epoch_cycles == 0
            || settings.destination_batch_size == 0
            || settings.df_payload_size < 24
            || settings.df_payload_size > 65507
            || settings.port_batches.is_empty()
            || settings.port_batches.iter().any(Vec::is_empty)
            || settings.coverage_epoch_seconds
                != settings.coverage_epoch_cycles as u64 * settings.cycle_interval_seconds
            || settings.complete_window_seconds < settings.coverage_epoch_seconds
        {
            bail!("invalid compiled Pingmesh plan settings");
        }
        let mut indices: Vec<_> = settings.port_batches.iter().flatten().copied().collect();
        indices.sort_unstable();
        if indices != (0..settings.source_ports.len()).collect::<Vec<_>>() {
            bail!("port batches must partition the source-port pool");
        }
        Ok(())
    }
}

impl ClientProbePlan {
    fn validate(&self, config: &ClientAgentConfig, local: &Client) -> Result<()> {
        let settings = &config.pingmesh_plan;
        let names: HashSet<_> = config.clients.iter().map(|client| &client.name).collect();
        if self.schema_version != CONFIG_SCHEMA_VERSION
            || self.plan_id != settings.plan_id
            || self.name != local.name
            || self.frames.len() != settings.coverage_epoch_cycles
            || self.targets.is_empty()
            || self.targets.iter().collect::<HashSet<_>>().len() != self.targets.len()
            || self
                .targets
                .iter()
                .any(|name| name == &local.name || !names.contains(name))
            || self.startup_delay_ms >= settings.cycle_interval_seconds * 1000
        {
            bail!("client plan does not match current configuration");
        }
        let mut coverage = HashSet::new();
        for frame in &self.frames {
            let batch = settings
                .port_batches
                .get(frame.port_batch_index)
                .context("invalid frame port batch")?;
            if frame.target_indices.len() > settings.destination_batch_size
                || frame
                    .target_indices
                    .iter()
                    .any(|index| *index >= self.targets.len())
                || frame.target_indices.iter().collect::<HashSet<_>>().len()
                    != frame.target_indices.len()
            {
                bail!("invalid frame targets");
            }
            let mut rtt = HashSet::new();
            let mut df = HashSet::new();
            for (kind, target, socket) in &frame.probes {
                if *target >= frame.target_indices.len() || *socket >= batch.len() {
                    bail!("probe references an invalid target or socket");
                }
                match kind {
                    ProbeKind::Rtt => {
                        if !rtt.insert((*target, *socket)) {
                            bail!("duplicate RTT task");
                        }
                    }
                    ProbeKind::Df => {
                        if !df.insert(*target) {
                            bail!("duplicate DF task");
                        }
                    }
                }
            }
            if rtt.len() != frame.target_indices.len() * batch.len()
                || df.len() != frame.target_indices.len()
            {
                bail!("frame is missing RTT or DF tasks");
            }
            for target in &frame.target_indices {
                if !coverage.insert((*target, frame.port_batch_index)) {
                    bail!("duplicate target/port batch");
                }
            }
        }
        if coverage.len() != self.targets.len() * settings.port_batches.len() {
            bail!("client plan has incomplete target/port coverage");
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> (ClientAgentConfig, ClientProbePlan) {
        let config: ClientAgentConfig = serde_json::from_value(serde_json::json!({
            "schema_version": 2, "topology_id": "test", "clients": [
                {"name":"a","data_ip":"192.0.2.1","management_ip":"172.20.0.1","rack":"r1","leaf":"l1"},
                {"name":"b","data_ip":"192.0.2.2","management_ip":"172.20.0.2","rack":"r2","leaf":"l2"}
            ], "client_plan_sha256":{"a":"digest", "b":"digest"},
            "pingmesh_plan": {"plan_id":"a".repeat(64), "source_ports":[33000,33001], "port_batches":[[0,1]],
                "destination_batch_size":1, "cycle_interval_seconds":1, "coverage_epoch_cycles":1,
                "coverage_epoch_seconds":1,"complete_window_seconds":6,"df_payload_size":1400}
        })).unwrap();
        let plan: ClientProbePlan = serde_json::from_value(serde_json::json!({
            "schema_version":2,"plan_id":"a".repeat(64),"name":"a","targets":["b"],"startup_delay_ms":5,
            "frames":[{"destination_batch_index":0,"port_batch_index":0,"target_indices":[0],
                "probes":[["rtt",0,0],["rtt",0,1],["df",0,0]]}]
        })).unwrap();
        (config, plan)
    }
    #[test]
    fn current_plan_validates_and_rejects_missing_duplicate_or_wrong_tasks() {
        let (config, plan) = fixture();
        config.validate().unwrap();
        plan.validate(&config, &config.clients[0]).unwrap();
        let mut missing = plan.clone();
        missing.frames[0].probes.pop();
        assert!(missing.validate(&config, &config.clients[0]).is_err());
        let mut duplicate = plan.clone();
        duplicate.frames[0].probes.push((ProbeKind::Rtt, 0, 0));
        assert!(duplicate.validate(&config, &config.clients[0]).is_err());
        let mut wrong = plan;
        wrong.targets[0] = "unknown".into();
        assert!(wrong.validate(&config, &config.clients[0]).is_err());
    }
    #[test]
    fn stale_versions_and_mismatched_plan_ids_are_rejected() {
        let (mut config, mut plan) = fixture();
        config.schema_version = 1;
        assert!(config.validate().is_err());
        config.schema_version = 2;
        plan.plan_id = "b".repeat(64);
        assert!(plan.validate(&config, &config.clients[0]).is_err());
    }
}
