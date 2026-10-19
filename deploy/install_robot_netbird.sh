#!/bin/bash
# Interactive Mac entry point; SSH/sudo passwords stay in the user's terminal.
set -euo pipefail
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
test -f "$source_dir/netbird_nx/install.sh"
test -f "$source_dir/netbird_rk/install.sh"

echo '1/2: Copy and install NX video forwarding (100.67.34.120).'
scp -r "$source_dir/netbird_nx" robot@100.67.34.120:~/
ssh -t robot@100.67.34.120 'sudo bash ~/netbird_nx/install.sh'

echo '2/2: Copy and install RK persistent Mac route (192.168.168.168).'
scp -r "$source_dir/netbird_rk" robot@192.168.168.168:~/
ssh -t robot@192.168.168.168 'sudo bash ~/netbird_rk/install.sh'

echo 'Both installers completed. Check their output and test video before rebooting.'
