from __future__ import annotations

import sys

from diagnose_smsm_single_target_lookup import _run_single_certificate_workflow_cli


def main() -> int:
    return _run_single_certificate_workflow_cli(
        ["--prepare-smsm-certificate-upload", *sys.argv[1:]]
    )


if __name__ == "__main__":
    raise SystemExit(main())