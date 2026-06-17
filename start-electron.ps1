$ErrorActionPreference = "Stop"

Set-Location -LiteralPath $PSScriptRoot

if (-not (Test-Path -LiteralPath "node_modules\electron")) {
    npm install
}

npm run electron
