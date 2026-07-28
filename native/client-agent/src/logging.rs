use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};

const MAX_LOG_BYTES: u64 = 10 * 1024 * 1024;
const ROTATED_LOGS: usize = 2;

static LOG_PATH: OnceLock<PathBuf> = OnceLock::new();
static LOG_LOCK: Mutex<()> = Mutex::new(());

pub fn init(path: impl Into<PathBuf>) {
    let _ = LOG_PATH.set(path.into());
}

pub fn error(message: impl AsRef<str>) {
    let Some(path) = LOG_PATH.get() else {
        return;
    };
    let Ok(_guard) = LOG_LOCK.lock() else {
        return;
    };
    if path
        .metadata()
        .is_ok_and(|metadata| metadata.len() >= MAX_LOG_BYTES)
    {
        rotate(path);
    }
    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(file, "{}", message.as_ref());
    }
}

fn rotate(path: &Path) {
    let oldest = path.with_extension(format!("log.{ROTATED_LOGS}"));
    let _ = fs::remove_file(oldest);
    for index in (1..ROTATED_LOGS).rev() {
        let source = path.with_extension(format!("log.{index}"));
        let destination = path.with_extension(format!("log.{}", index + 1));
        let _ = fs::rename(source, destination);
    }
    let _ = fs::rename(path, path.with_extension("log.1"));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rotation_names_are_bounded() {
        let path = Path::new("/tmp/pingmesh.log");
        assert_eq!(
            path.with_extension("log.1"),
            Path::new("/tmp/pingmesh.log.1")
        );
        assert_eq!(
            path.with_extension("log.2"),
            Path::new("/tmp/pingmesh.log.2")
        );
    }
}
