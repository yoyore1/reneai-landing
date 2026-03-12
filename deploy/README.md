# Deploy

## Auto-restart test bot (systemd)

To run the test bot under systemd so it auto-restarts if it stops:

```bash
# On EC2
sudo cp deploy/s3-testbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable s3-testbot
sudo systemctl start s3-testbot
sudo systemctl status s3-testbot
```

The launcher will still work for Start/Stop. When using systemd, stop the screen-based bot first, then use `systemctl start s3-testbot` instead of the launcher's Start Test Bot (or disable the launcher's test bot controls and use systemctl).

Logs: `journalctl -u s3-testbot -f` or `tail -f ~/reneai-landing/test_bot.log`
