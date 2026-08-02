import subprocess


def run_report(command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, shell=True, text=True)
