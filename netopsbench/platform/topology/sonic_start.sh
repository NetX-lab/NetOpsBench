#!/bin/bash -e

# NetOpsBench wrapper for yyyyyt123/netopsbench-sonic-vs-202505-telemetry:202505-telemetry.
# Original /usr/bin/start.sh sha256:
NETOPSBENCH_ORIGINAL_SONIC_START_SHA256=8c5aa959f0a3ed0bf1a57f7ecfd004485d5600b9ab71b388c2b15e109b77ee12

wait_for_front_panel_links() {
    local hwsku_dir="/usr/share/sonic/device/$PLATFORM/$HWSKU"
    local lanemap_file="$hwsku_dir/lanemap.ini"
    local timeout=600
    local interval=1
    local expected actual started now

    if [ ! -r "$lanemap_file" ]; then
        echo "NetOpsBench SONiC startup: missing readable lanemap.ini: $lanemap_file" >&2
        return 1
    fi

    expected=$(grep -Ec '^eth[0-9]+:' "$lanemap_file" || true)
    if [ "$expected" -le 0 ]; then
        echo "NetOpsBench SONiC startup: no front-panel ports found in $lanemap_file" >&2
        return 1
    fi

    started=$(date +%s)
    while true; do
        actual=$(front_panel_links | wc -l)
        if [ "$actual" -ge "$expected" ]; then
            echo "NetOpsBench SONiC startup: detected $actual/$expected front-panel links"
            return 0
        fi

        now=$(date +%s)
        if [ $((now - started)) -ge "$timeout" ]; then
            echo "NetOpsBench SONiC startup: timed out waiting for front-panel links ($actual/$expected)" >&2
            return 1
        fi
        sleep "$interval"
    done
}

front_panel_links() {
    local path name

    for path in /sys/class/net/eth[0-9]*; do
        [ -e "$path" ] || continue
        name=${path##*/}
        [ "$name" = "eth0" ] || printf '%s\n' "$name"
    done
}

install_generated_config_db() {
    local src="$1"
    local dst="$2"

    if mv "$src" "$dst"; then
        return 0
    fi

    # /etc/sonic/config_db.json is a NetOpsBench bind mount.  Linux will not
    # replace a mounted file with rename(2), so preserve the mount and update
    # the contents in place.
    cat "$src" > "$dst"
    rm -f "$src"
}

# Generate configuration

# NOTE: 'PLATFORM' and 'HWSKU' environment variables are set
# in the Dockerfile so that they persist for the life of the container

ln -sf /usr/share/sonic/device/$PLATFORM /usr/share/sonic/platform
ln -sf /usr/share/sonic/device/$PLATFORM/$HWSKU /usr/share/sonic/hwsku

SWITCH_TYPE=switch
PLATFORM_CONF=platform.json
if [[ $HWSKU == "DPU-2P" ]]; then
    SWITCH_TYPE=dpu
    PLATFORM_CONF=platform-dpu-2p.json
fi

wait_for_front_panel_links

pushd /usr/share/sonic/hwsku

# filter available front panel ports in lanemap.ini
[ -f lanemap.ini.orig ] || cp lanemap.ini lanemap.ini.orig
for p in $(front_panel_links); do
    grep ^$p: lanemap.ini.orig
done > lanemap.ini

# filter available sonic front panel ports in port_config.ini
[ -f port_config.ini.orig ] || cp port_config.ini port_config.ini.orig
grep ^# port_config.ini.orig > port_config.ini
for lanes in $(awk -F ':' '{print $2}' lanemap.ini); do
    grep -E "\s$lanes\s" port_config.ini.orig
done >> port_config.ini

popd

[ -d /etc/sonic ] || mkdir -p /etc/sonic

# Note: libswsscommon requires a dabase_config file in /var/run/redis/sonic-db/
# Prepare this file before any dependent application, such as sonic-cfggen
mkdir -p /var/run/redis/sonic-db
cp /etc/default/sonic-db/database_config.json /var/run/redis/sonic-db/

SYSTEM_MAC_ADDRESS=$(cat /sys/class/net/eth0/address)
sonic-cfggen -t /usr/share/sonic/templates/init_cfg.json.j2 -a "{\"system_mac\": \"$SYSTEM_MAC_ADDRESS\", \"switch_type\": \"$SWITCH_TYPE\"}" > /etc/sonic/init_cfg.json

if [[ -f /usr/share/sonic/virtual_chassis/default_config.json ]]; then
    sonic-cfggen -j /etc/sonic/init_cfg.json -j /usr/share/sonic/virtual_chassis/default_config.json --print-data > /tmp/init_cfg.json
    mv /tmp/init_cfg.json /etc/sonic/init_cfg.json
fi

if [ -f /etc/sonic/config_db.json ]; then
    sonic-cfggen -j /etc/sonic/init_cfg.json -j /etc/sonic/config_db.json --print-data > /tmp/config_db.json
    install_generated_config_db /tmp/config_db.json /etc/sonic/config_db.json
else
    # generate and merge buffers configuration into config file
    if [ -f /usr/share/sonic/hwsku/buffers.json.j2 ]; then
        sonic-cfggen -k $HWSKU -p /usr/share/sonic/device/$PLATFORM/$PLATFORM_CONF -t /usr/share/sonic/hwsku/buffers.json.j2 > /tmp/buffers.json
        buffers_cmd="-j /tmp/buffers.json"
    fi
    if [ -f /usr/share/sonic/hwsku/qos.json.j2 ]; then
        sonic-cfggen -j /etc/sonic/init_cfg.json -t /usr/share/sonic/hwsku/qos.json.j2 > /tmp/qos.json
        qos_cmd="-j /tmp/qos.json"
    fi

    sonic-cfggen -p /usr/share/sonic/device/$PLATFORM/$PLATFORM_CONF -k $HWSKU --print-data > /tmp/ports.json
    # change admin_status from up to down; Test cases dependent
    sed -i "s/up/down/g" /tmp/ports.json
    sonic-cfggen -j /etc/sonic/init_cfg.json $buffers_cmd $qos_cmd -j /tmp/ports.json --print-data > /etc/sonic/config_db.json
fi

sonic-cfggen -t /usr/share/sonic/templates/copp_cfg.j2 > /etc/sonic/copp_cfg.json

if [ "$HWSKU" == "Mellanox-SN2700" ]; then
    cp /usr/share/sonic/hwsku/sai_mlnx.profile /usr/share/sonic/hwsku/sai.profile
elif [ "$HWSKU" == "DPU-2P" ]; then
    cp /usr/share/sonic/hwsku/sai_dpu_2p.profile /usr/share/sonic/hwsku/sai.profile
fi

mkdir -p /etc/swss/config.d/

rm -f /var/run/rsyslogd.pid

supervisorctl start rsyslogd

supervisord_cfg="/etc/supervisor/conf.d/supervisord.conf"
chassisdb_cfg_file="/usr/share/sonic/virtual_chassis/default_config.json"
chassisdb_cfg_file_default="/etc/default/sonic-db/default_chassis_cfg.json"
host_template="/usr/share/sonic/templates/hostname.j2"
db_cfg_file="/var/run/redis/sonic-db/database_config.json"
db_cfg_file_tmp="/var/run/redis/sonic-db/database_config.json.tmp"

if [ -r "$chassisdb_cfg_file" ]; then
   echo $(sonic-cfggen -j $chassisdb_cfg_file -t $host_template) >> /etc/hosts
else
   chassisdb_cfg_file="$chassisdb_cfg_file_default"
   echo "10.8.1.200 redis_chassis.server" >> /etc/hosts
fi

supervisorctl start redis-server

start_chassis_db=`sonic-cfggen -v DEVICE_METADATA.localhost.start_chassis_db -y $chassisdb_cfg_file`
if [[ "$HOSTNAME" == *"supervisor"* ]] || [ "$start_chassis_db" == "1" ]; then
   supervisorctl start redis-chassis
fi

conn_chassis_db=`sonic-cfggen -v DEVICE_METADATA.localhost.connect_to_chassis_db -y $chassisdb_cfg_file`
if [ "$start_chassis_db" != "1" ] && [ "$conn_chassis_db" != "1" ]; then
   cp $db_cfg_file $db_cfg_file_tmp
   update_chassisdb_config -j $db_cfg_file_tmp -d
   cp $db_cfg_file_tmp $db_cfg_file
fi

if [ "$conn_chassis_db" == "1" ]; then
   if [ -f /usr/share/sonic/virtual_chassis/coreportindexmap.ini ]; then
      cp /usr/share/sonic/virtual_chassis/coreportindexmap.ini /usr/share/sonic/hwsku/

      pushd /usr/share/sonic/hwsku

      # filter available front panel ports in coreportindexmap.ini
      [ -f coreportindexmap.ini.orig ] || cp coreportindexmap.ini coreportindexmap.ini.orig
      for p in $(front_panel_links); do
          grep ^$p: coreportindexmap.ini.orig
      done > coreportindexmap.ini

      popd
   fi
fi

/usr/bin/configdb-load.sh

if [ "$HWSKU" = "brcm_gearbox_vs" ]; then
    supervisorctl start gbsyncd
    supervisorctl start gearsyncd
fi

supervisorctl start syncd

supervisorctl start portsyncd

supervisorctl start orchagent

supervisorctl start coppmgrd
supervisorctl start neighsyncd
supervisorctl start fdbsyncd
supervisorctl start teamsyncd
supervisorctl start fpmsyncd
supervisorctl start teammgrd
supervisorctl start vrfmgrd
supervisorctl start portmgrd
supervisorctl start intfmgrd
supervisorctl start vlanmgrd
supervisorctl start zebra
supervisorctl start mgmtd
supervisorctl start staticd
supervisorctl start buffermgrd
supervisorctl start nbrmgrd
supervisorctl start vxlanmgrd
supervisorctl start natmgrd
supervisorctl start natsyncd
supervisorctl start tunnelmgrd
supervisorctl start fabricmgrd

# This pinned VS image omits rebootbackend. Skip only an absent service;
# all existing service start failures still propagate under bash -e.
if python3 - <<'CHECK_REBOOTBACKEND'
from supervisor.options import ClientOptions
import sys
options = ClientOptions()
options.realize(args=[])
with options.getServerProxy() as proxy:
    names = {p['name'] for p in proxy.supervisor.getAllProcessInfo()}
sys.exit(0 if 'rebootbackend' in names else 3)
CHECK_REBOOTBACKEND
then
    supervisorctl start rebootbackend
else
    status=$?
    if [ "$status" -ne 3 ]; then exit "$status"; fi
    echo "NetOpsBench: optional rebootbackend is absent in this VS image"
fi

# Start arp_update when VLAN exists
VLAN=`sonic-cfggen -d -v 'VLAN.keys() | join(" ") if VLAN'`
if [ "$VLAN" != "" ]; then
    supervisorctl start arp_update
fi
