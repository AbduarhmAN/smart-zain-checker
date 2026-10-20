# Smart Zain Checker — VPS source baseline

This branch records the application source captured from the running VPS on 2026-10-20. Runtime credentials, worker/proxy configuration, customer files, wallets, payments, queues and checkpoints are excluded. Existing production state must be preserved during deployment.

The legacy Telegram controller reads its token from `ZAIN_TELEGRAM_BOT_TOKEN` or `TELEGRAM_BOT_TOKEN`. No project build or compilation was run to create this snapshot.

Cross-file account/service scheduling is implemented and verified on a separate branch based on this baseline.
