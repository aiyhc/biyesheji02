param(
    [string]$IPAddress = ""
)

$ErrorActionPreference = "Stop"

$opensslCommand = Get-Command openssl -ErrorAction Stop
$openssl = $opensslCommand.Source

if (-not $IPAddress) {
    $route = Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
        Sort-Object RouteMetric |
        Select-Object -First 1
    if ($route) {
        $address = Get-NetIPAddress -InterfaceIndex $route.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.AddressState -eq "Preferred" -and $_.IPAddress -notlike "169.254.*" } |
            Select-Object -First 1
        if ($address) { $IPAddress = $address.IPAddress }
    }
}

if (-not $IPAddress) {
    throw "Could not detect the active Wi-Fi IPv4 address. Pass it with -IPAddress, for example -IPAddress 192.168.1.20."
}

$parsedAddress = [System.Net.IPAddress]::Parse($IPAddress)
$octets = $parsedAddress.GetAddressBytes()
$isPrivate = $parsedAddress.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork -and (
    $octets[0] -eq 10 -or
    ($octets[0] -eq 172 -and $octets[1] -ge 16 -and $octets[1] -le 31) -or
    ($octets[0] -eq 192 -and $octets[1] -eq 168)
)
if (-not $isPrivate) {
    throw "Use the laptop's private Wi-Fi IPv4 address, not a public or loopback address."
}

$httpsDirectory = Join-Path $PSScriptRoot ".https"
New-Item -ItemType Directory -Path $httpsDirectory -Force | Out-Null
$caKey = Join-Path $httpsDirectory "local-ca-key.pem"
$caPem = Join-Path $httpsDirectory "local-ca.pem"
$caDer = Join-Path $httpsDirectory "local-ca.cer"
$serverKey = Join-Path $httpsDirectory "gateway-key.pem"
$serverCsr = Join-Path $httpsDirectory "gateway.csr"
$serverCert = Join-Path $httpsDirectory "gateway-cert.pem"
$extensions = Join-Path $httpsDirectory "gateway.ext"

if (-not (Test-Path $caKey) -and -not (Test-Path $caPem)) {
    & $openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 3650 `
        -keyout $caKey -out $caPem -subj "/CN=ESP32 Gateway Local CA" `
        -addext "basicConstraints=critical,CA:TRUE" `
        -addext "keyUsage=critical,keyCertSign,cRLSign"
    if ($LASTEXITCODE -ne 0) { throw "Failed to create the local certificate authority." }
} elseif (-not (Test-Path $caKey) -or -not (Test-Path $caPem)) {
    throw "The local CA files are incomplete in $httpsDirectory. Do not delete them individually; restore both or remove the .https folder and rerun this script."
}

& $openssl req -newkey rsa:2048 -nodes -keyout $serverKey -out $serverCsr -subj "/CN=$IPAddress"
if ($LASTEXITCODE -ne 0) { throw "Failed to create the gateway certificate request." }

$extensionText = @"
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=IP:$IPAddress,IP:127.0.0.1,DNS:localhost
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
"@
[System.IO.File]::WriteAllText($extensions, $extensionText, [System.Text.Encoding]::ASCII)

& $openssl x509 -req -in $serverCsr -CA $caPem -CAkey $caKey -CAcreateserial `
    -out $serverCert -days 825 -sha256 -extfile $extensions
if ($LASTEXITCODE -ne 0) { throw "Failed to sign the gateway certificate." }

& $openssl x509 -in $caPem -outform DER -out $caDer
if ($LASTEXITCODE -ne 0) { throw "Failed to create the CA certificate for device installation." }

Write-Host "HTTPS certificate created for $IPAddress."
Write-Host "Certificate: $serverCert"
Write-Host "Private key: $serverKey"
Write-Host "CA certificate to install on the phone: $caDer"
Write-Host "Keep local-ca-key.pem private; do not send it to the phone or share it."
Write-Host ""
Write-Host "Start the gateway from host_gateway with:"
Write-Host "python gateway.py --host $IPAddress --port 8443 --tls-certfile .https/gateway-cert.pem --tls-keyfile .https/gateway-key.pem"
Write-Host ""
Write-Host "Install local-ca.cer on the phone as a CA certificate before opening https://$($IPAddress):8443/."
