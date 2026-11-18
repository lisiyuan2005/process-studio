#!/bin/bash
# 下载 lsvox 编译所需的头文件，版本与 ViennaPS 4.7.0 wheel 一致
set -e
cd "$(dirname "$0")/.." && mkdir -p third_party && cd third_party
[ -d ViennaLS ]   || git clone -q --depth 1 --branch v5.10.0 https://github.com/ViennaTools/ViennaLS.git
[ -d ViennaHRLE ] || git clone -q --depth 1 --branch v1.1.2  https://github.com/ViennaTools/ViennaHRLE.git
[ -d ViennaCore ] || git clone -q --depth 1 --branch v2.2.1  https://github.com/ViennaTools/ViennaCore.git
echo "third_party ready"
