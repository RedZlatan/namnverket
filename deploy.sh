#!/bin/bash
echo "Pushar till git..."
cd ~/Desktop/namnge
git add .
git commit -m "${1:-uppdatering}"
git push

echo "Deploying till servern..."
ssh root@178.105.219.51 "cd /var/www/namnverket && git pull && systemctl restart namnverket"

echo "✅ Deploy klar! https://namnverket.se"
