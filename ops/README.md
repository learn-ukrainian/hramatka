# Hramatka ops

Pilot deploy and smoke helpers. From a reviewed checkout:

`./hramatka/ops/deploy.sh <user@pilot-host>`

`deploy.sh` replaces the hand-rolled partial rsync (app dist + engine + api). It builds with CSP guard, rsyncs the whole `hramatka` package (explicit excludes), blocks restart during in-flight bakes unless `--force`, then restarts, polls readyz, prints migration version, runs `smoke.sh`, and prints bundle hash + git SHA. Preview locally: `./hramatka/ops/deploy.sh --dry-run /tmp/fake-deploy`.
