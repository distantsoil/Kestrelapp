<#
.SYNOPSIS
    Decide whether one file of ours is signed as the signing mode requires (D-REL-18za, 18zc).

.DESCRIPTION
    Dot-sourced by both signing jobs before their upload and by windows-verify after the
    install, so the checks cannot drift. Get-KestrelSignatureProblem returns $null when the
    file passes, otherwise one line naming the file and its status.

    production: Status is Valid, the signer name equals the expected publisher exactly, and a
                timestamp is present.
    test:       Status is not Valid; Status is UnknownError or NotTrusted (SignatureStatus has
                no UntrustedRoot member); a timestamp is present. The signer and its chain are
                printed, not judged, until a dry run shows what a test certificate looks like.
#>

function Write-KestrelTestSignatureDiagnostics {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Signature,
        [Parameter(Mandatory = $true)][string]$Publisher
    )
    Write-Host "----- BEGIN TEST SIGNATURE DIAGNOSTICS: $Path -----"
    # Diagnostics only: nothing here decides a result, and an error is printed, never raised.
    try {
        Write-Host "status: $($Signature.Status)"
        $cert = $Signature.SignerCertificate
        if ($cert) {
            $name = $cert.GetNameInfo([System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false)
            Write-Host "signer name: $name (equals ARTIFACT_SIGNING_PUBLISHER: $($name -ceq $Publisher))"
            Write-Host "signer subject: $($cert.Subject)"
            Write-Host "signer issuer: $($cert.Issuer)"
            Write-Host "signer thumbprint: $($cert.Thumbprint)"
            # The chain from the file's embedded certificates, revocation not checked.
            $chain = [System.Security.Cryptography.X509Certificates.X509Chain]::new()
            try {
                $chain.ChainPolicy.RevocationMode = [System.Security.Cryptography.X509Certificates.X509RevocationMode]::NoCheck
                $embedded = [System.Security.Cryptography.X509Certificates.X509Certificate2Collection]::new()
                $embedded.Import($Path)
                [void]$chain.ChainPolicy.ExtraStore.AddRange($embedded)
                $built = $chain.Build($cert)
                Write-Host "chain built: $built; flags: $(@($chain.ChainStatus | ForEach-Object { $_.Status }) -join ', ')"
                foreach ($element in $chain.ChainElements) {
                    Write-Host "chain element: $($element.Certificate.Subject) | thumbprint $($element.Certificate.Thumbprint)"
                }
            } catch {
                # A chain that cannot be built is reported here, so the lines below still print.
                Write-Host "chain: not built: $($_.Exception.Message)"
            } finally {
                $chain.Dispose()
            }
        } else {
            Write-Host "signer: none"
        }
        if ($Signature.TimeStamperCertificate) { Write-Host "timestamp: $($Signature.TimeStamperCertificate.Subject)" } else { Write-Host "timestamp: none" }
    } catch {
        Write-Host "diagnostics stopped: $($_.Exception.Message)"
    }
    Write-Host "----- END TEST SIGNATURE DIAGNOSTICS -----"
}

function Get-KestrelSignatureProblem {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][ValidateSet("production", "test")][string]$Mode,
        [Parameter(Mandatory = $true)][string]$Publisher
    )
    $sig = Get-AuthenticodeSignature -LiteralPath $Path
    $what = "${Path}: status $($sig.Status)"
    if ($Mode -eq "test") {
        Write-KestrelTestSignatureDiagnostics -Path $Path -Signature $sig -Publisher $Publisher
        if ($sig.Status -eq "Valid") { return "$what; a test-profile signature must not be publicly trusted; the test profile is publicly trusted or misconfigured" }
        if (@("UnknownError", "NotTrusted") -notcontains $sig.Status) { return "$what; a test signature must be UnknownError or NotTrusted" }
        if (-not $sig.TimeStamperCertificate) { return "$what; no timestamp" }
        return $null
    }
    if ($sig.Status -ne "Valid") { return "$what; a release signature must be Valid" }
    $name = ""
    if ($sig.SignerCertificate) { $name = $sig.SignerCertificate.GetNameInfo([System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false) }
    if ($name -cne $Publisher) { return "$what; signed by '$name', expected '$Publisher'" }
    if (-not $sig.TimeStamperCertificate) { return "$what; no timestamp" }
    return $null
}
