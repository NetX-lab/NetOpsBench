mod config;
mod control;
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
        Command::Pingmesh { config } => pingmesh::run(&config).await,
        Command::Traffic { config } => traffic::run(&config).await,
    }
}
