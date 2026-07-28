mod config;
mod control;
mod logging;
mod pingmesh;
mod traffic;

use std::path::PathBuf;

use anyhow::Result;
use clap::{Parser, Subcommand};

#[derive(Debug, Parser)]
#[command(name = "netopsbench-client-agent", version)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    Pingmesh {
        #[arg(long)]
        config: PathBuf,
    },
    Traffic {
        #[arg(long)]
        config: PathBuf,
    },
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Command::Pingmesh { config } => {
            logging::init("/var/log/netopsbench/pingmesh.log");
            log_command_result(pingmesh::run(&config).await)
        }
        Command::Traffic { config } => {
            logging::init("/var/log/netopsbench/traffic.log");
            log_command_result(traffic::run(&config).await)
        }
    }
}

fn log_command_result(result: Result<()>) -> Result<()> {
    if let Err(error) = &result {
        logging::error(format!("client agent terminated: {error:#}"));
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn top_level_failure_is_written_to_the_bounded_log() {
        let path = std::env::temp_dir().join(format!(
            "netopsbench-client-agent-{}-fatal.log",
            std::process::id()
        ));
        let _ = std::fs::remove_file(&path);
        logging::init(&path);

        assert!(log_command_result(Err(anyhow::anyhow!("startup failed"))).is_err());
        assert!(
            std::fs::read_to_string(&path)
                .unwrap()
                .contains("client agent terminated: startup failed")
        );
        let _ = std::fs::remove_file(path);
    }
}
