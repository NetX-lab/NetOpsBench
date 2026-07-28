use std::fs;
use std::net::Ipv4Addr;
use std::path::Path;

use anyhow::{Context, Result, bail};
use serde::Deserialize;

pub const CONFIG_SCHEMA_VERSION: u32 = 1;
pub const CONTROL_PROTOCOL_VERSION: u32 = 1;
pub const PINGMESH_CONTROL_PORT: u16 = 9910;
pub const TRAFFIC_CONTROL_PORT: u16 = 9911;

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ClientAgentConfig {
    pub schema_version: u32,
    pub topology_id: String,
    pub pingmesh_policy: PingmeshPolicy,
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
pub struct PingmeshPolicy {
    pub destination_batch_size: Option<usize>,
    pub rtt_port_pool_size: usize,
    pub rtt_ports_per_cycle: usize,
    pub cycle_interval_seconds: f64,
    pub destination_batch_count: usize,
    pub port_batch_count: usize,
    pub coverage_epoch_cycles: u64,
    pub coverage_epoch_seconds: u64,
    pub df_payload_size: usize,
}

impl ClientAgentConfig {
    pub fn load(path: &Path) -> Result<Self> {
        let bytes = fs::read(path).with_context(|| format!("read {}", path.display()))?;
        let config: Self =
            serde_json::from_slice(&bytes).with_context(|| format!("parse {}", path.display()))?;
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

    fn validate(&self) -> Result<()> {
        if self.schema_version != CONFIG_SCHEMA_VERSION {
            bail!(
                "unsupported client-agent schema version {}; expected {}",
                self.schema_version,
                CONFIG_SCHEMA_VERSION
            );
        }
        if self.clients.len() < 2 {
            bail!("client-agent config requires at least two clients");
        }
        let destinations = self.clients.len() - 1;
        let batch_size = self
            .pingmesh_policy
            .destination_batch_size
            .unwrap_or(destinations)
            .clamp(1, destinations);
        let destination_batches = destinations.div_ceil(batch_size);
        let ports = self.pingmesh_policy.rtt_port_pool_size;
        let ports_per_cycle = self.pingmesh_policy.rtt_ports_per_cycle;
        if ports == 0 || ports_per_cycle == 0 || ports_per_cycle > ports {
            bail!("invalid Pingmesh source-port policy");
        }
        let port_batches = ports.div_ceil(ports_per_cycle);
        if destination_batches != self.pingmesh_policy.destination_batch_count
            || port_batches != self.pingmesh_policy.port_batch_count
            || (destination_batches * port_batches) as u64
                != self.pingmesh_policy.coverage_epoch_cycles
        {
            bail!("stale Pingmesh coverage policy; regenerate the topology");
        }
        if self.pingmesh_policy.cycle_interval_seconds <= 0.0 {
            bail!("Pingmesh cycle interval must be positive");
        }
        let expected_epoch_seconds = (self.pingmesh_policy.coverage_epoch_cycles as f64
            * self.pingmesh_policy.cycle_interval_seconds)
            .ceil() as u64;
        if self.pingmesh_policy.coverage_epoch_seconds != expected_epoch_seconds {
            bail!("stale Pingmesh coverage duration; regenerate the topology");
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn compact_config_is_linear_for_1024_clients() {
        let clients: Vec<_> = (1..=1024)
            .map(|index| {
                serde_json::json!({
                    "name": format!("client{index}"),
                    "data_ip": format!("10.{}.{}.{}", index / 65536, (index / 256) % 256, index % 256),
                    "management_ip": format!("172.20.{}.{}", (index / 254) % 256, index % 254 + 1),
                    "rack": format!("rack{}", index / 8),
                    "leaf": format!("leaf{}", index / 8)
                })
            })
            .collect();
        let payload = serde_json::json!({
            "schema_version": 1,
            "topology_id": "synthetic-1024",
            "pingmesh_policy": {
                "destination_batch_size": 16,
                "rtt_port_pool_size": 16,
                "rtt_ports_per_cycle": 4,
                "cycle_interval_seconds": 1.0,
                "destination_batch_count": 64,
                "port_batch_count": 4,
                "coverage_epoch_cycles": 256,
                "coverage_epoch_seconds": 256,
                "df_payload_size": 1400
            },
            "clients": clients
        });
        assert!(serde_json::to_vec(&payload).unwrap().len() < 1_000_000);
    }
}
