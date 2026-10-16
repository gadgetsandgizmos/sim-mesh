# sim.ps1: sim-mesh's front on Windows without a bash, with Docker or Podman.
# It does what `sim` with no arguments does: pulls the image once per
# Dockerfile (or builds it from the Dockerfile when it cannot be pulled),
# removes the images of earlier Dockerfiles, then runs `sim front` inside it
# with port 8800 published; inside, `sim front` fetches what sim-mesh compiles,
# prebuilt. Every other verb runs inside the front once it is up:
#   docker exec -it sim-mesh-front ./sim <verb>
# Start it from the directory the clone is in:
#   powershell -ExecutionPolicy Bypass -File sim-mesh\sim.ps1
# SIM_MESH_IMAGE_REPO and SIM_MESH_PREBUILT name other repositories for the
# image and the prebuilt outputs, as for `sim`.
$root = $PSScriptRoot
# Mounted as /mesh: sim mounts the clone's parent, where a firmware tree beside
# sim-mesh is found.
$outer = Split-Path -Parent $root
$engine = $env:SIM_MESH_ENGINE
if (-not $engine) {
    if (Get-Command podman -ErrorAction SilentlyContinue) { $engine = 'podman' }
    elseif (Get-Command docker -ErrorAction SilentlyContinue) { $engine = 'docker' }
    else { throw 'sim-mesh runs in a container, and neither podman nor docker is installed' }
}
& $engine info *> $null
if ($LASTEXITCODE -ne 0) { throw "$engine does not answer: is it running? (podman: podman machine start; docker: start Docker Desktop)" }
# sim is a bash script, which a checkout that turned its line ends into CRLF breaks.
if ((Get-Content -Raw (Join-Path $root 'sim')) -match "`r`n") {
    throw 'sim has Windows line ends: clone again with  git clone --config core.autocrlf=false https://github.com/sim-mesh/sim-mesh.git'
}
# The image's name is sim's: the repository, and the first 12 hex digits of
# the Dockerfile's sha256.
$repo = $env:SIM_MESH_IMAGE_REPO
if (-not $repo) { $repo = 'ghcr.io/sim-mesh/sim-mesh' }
$tag = (Get-FileHash -Algorithm SHA256 (Join-Path $root 'Dockerfile')).Hash.Substring(0, 12).ToLower()
$image = "${repo}:$tag"
& $engine image inspect $image *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "pulling the sim-mesh image ($image, about 50 MB)"
    & $engine pull $image
    if ($LASTEXITCODE -ne 0) {
        Write-Host "it cannot be pulled: building it from the Dockerfile with $engine..."
        & $engine build -t $image -f (Join-Path $root 'Dockerfile') $root
        if ($LASTEXITCODE -ne 0) { throw 'the sim-mesh image did not build' }
    }
}
# Every other sim-mesh image, by name, as sim removes them: an earlier
# Dockerfile's, and those named as sim-mesh named them before. Never a prune,
# which would take the user's other images with them; one in use refuses.
$removed = 0
foreach ($ref in (& $engine images --format '{{.Repository}}:{{.Tag}}')) {
    if ($ref -eq $image) { continue }
    if ($ref.StartsWith("${repo}:") -or $ref -like 'sim-mesh:*' -or $ref -like 'localhost/sim-mesh:*') {
        & $engine rmi $ref *> $null
        if ($LASTEXITCODE -eq 0) { $removed++ }
    }
}
if ($removed -gt 0) { Write-Host "removed $removed old sim-mesh image(s)" }
# One front at a time, as sim keeps it: a running one is said so, one left
# behind stopped is cleared out of the name's way.
if (& $engine ps -q -f 'name=^sim-mesh-front$') { throw "a front is already running (sim-mesh-front): to restart it, $engine stop sim-mesh-front first" }
if (& $engine ps -aq -f 'name=^sim-mesh-front$') { & $engine rm -f sim-mesh-front | Out-Null }
$pass = @()
foreach ($name in 'SIM_MESH_PREBUILT', 'SIM_MESH_IMAGE_REPO') {
    $value = [Environment]::GetEnvironmentVariable($name)
    if ($value) { $pass += @('-e', "$name=$value") }
}
Write-Host 'the page: http://localhost:8800/ (once the front says it is up; Ctrl-C stops it)'
& $engine run --rm -it --init --name sim-mesh-front `
    --sysctl net.ipv4.ip_unprivileged_port_start=0 `
    -p 8800:8800/tcp -p 8800:8800/udp `
    -v "${outer}:/mesh" -w /mesh/sim-mesh `
    -v sim-mesh-home:/home/sim-mesh -e HOME=/home/sim-mesh `
    -e SIM_MESH_IN_CONTAINER=1 -e SIM_MESH_CALLER_DIR=/mesh/sim-mesh `
    @pass `
    $image /mesh/sim-mesh/sim front
