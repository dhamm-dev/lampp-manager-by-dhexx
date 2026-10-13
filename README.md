# LAMPP Project Controller
Manager lokal buat project di `/opt/lampp/htdocs` — Design A (dense utility).

## Jalanin
```bash
project-control                 # buka dashboard (auto-start service)
project-control edit (folder)   # buka project di Antigravity
project-control edit (folder) --subl  # buka project di Sublime
project-control status
project-control logs
```

Manual:
```bash
~/.local/bin/project-control
systemctl --user start lampp-manager
http://localhost:8888  (login pakai user di .env)
```

## Setup sekali
```bash
sudo cp ~/Projects/lampp-manager/sudoers-lampp-manager /etc/sudoers.d/lampp-manager
sudo chmod 440 /etc/sudoers.d/lampp-manager
systemctl --user daemon-reload
systemctl --user enable --now lampp-manager
```

## Ganti password
```bash
.venv/bin/python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('baru'))"
# paste ke .env ADMIN_PASS_HASH, lalu systemctl --user restart lampp-manager
```
