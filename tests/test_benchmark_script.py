from pathlib import Path


def test_benchmark_script_does_not_globally_clean_runtime_resources():
    repo_root = Path(__file__).resolve().parents[1]
    script = (repo_root / "scripts" / "run_all_benchmarks.sh").read_text(encoding="utf-8")

    forbidden = (
        "docker rm",
        "docker network rm",
        'rm -rf "$REPO_ROOT/.netopsbench/runtimes/',
        "runtime teardown --all",
    )
    assert all(fragment not in script for fragment in forbidden)
    assert "runtime teardown <runtime-name>" in script
