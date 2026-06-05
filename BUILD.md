# Build

## Development

Requirements:

- Python 3.12+
- Node.js 18+
- Chrome or Edge

Install the browser reader dependency:

```powershell
npm install
```

Run tests:

```powershell
python -m unittest -v
node test_usage_reader.js
node --check codex_usage_reader.js
```

Start the app:

```powershell
python quota_guard.py
```

## Windows executable

Install PyInstaller and build the desktop app:

```powershell
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --clean --windowed --onedir --name CodexQuotaGuard quota_guard.py
```

For a portable distribution, place these files beside `CodexQuotaGuard.exe`:

- `codex_usage_reader.js`
- `node.exe`
- `node_modules/playwright-core/`
- `README.md`
- `THIRD_PARTY_NOTICES.txt`

The published GitHub Release contains prebuilt Windows packages.

If a portable package does not include `node_modules`, run `install_browser_reader.bat`
once in the extracted directory before using web sync.
