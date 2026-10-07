#!/usr/bin/env bash
# One-command install for an Ubuntu server (e.g. Oracle Cloud Always Free).
#   bash <(curl -fsSL https://raw.githubusercontent.com/smallkhk/Make-me-money/claude/paper-trading-crawler/setup-server.sh)
# Runs the bot nonstop as a service: restarts on crash and on reboot.
set -euo pipefail
REPO="smallkhk/Make-me-money"
BRANCH="claude/paper-trading-crawler"
DIR="$HOME/desk"

echo "Paste your GitHub token (input is hidden), then press Enter:"
read -rs TOKEN < /dev/tty
echo
[ -n "$TOKEN" ] || { echo "No token given, stopping."; exit 1; }

echo "Installing git and python..."
sudo apt-get update -qq
sudo apt-get install -y -qq git python3 > /dev/null

if [ -d "$DIR/.git" ]; then
  git -C "$DIR" remote set-url origin "https://x-access-token:$TOKEN@github.com/$REPO.git"
  git -C "$DIR" pull -q
else
  git clone -q -b "$BRANCH" "https://x-access-token:$TOKEN@github.com/$REPO.git" "$DIR"
fi
git -C "$DIR" config user.name "paper-crawler"
git -C "$DIR" config user.email "paper-crawler@users.noreply.github.com"
git -C "$DIR" push -q --dry-run || { echo "That token can't push to $REPO. Check it has Contents: Read and write."; exit 1; }

sudo tee /etc/systemd/system/desk.service > /dev/null <<UNIT
[Unit]
Description=\$10 Desk paper-trading bot
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 -u crawler.py --serve --sync
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now desk
sleep 8
echo
echo "Bot is running. Last lines of its log:"
sudo journalctl -u desk -n 8 --no-pager
echo
echo "Useful commands:"
echo "  sudo journalctl -u desk -f      # watch it live (Ctrl+C to stop watching)"
echo "  sudo systemctl restart desk     # restart it"
echo "  sudo systemctl stop desk        # stop it"
