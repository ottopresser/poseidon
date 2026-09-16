RCD_SCRIPT_TEMPLATE = """#!/bin/sh
#
# PROVIDE: {service_name}
# REQUIRE: LOGIN NETWORKING
# KEYWORD: shutdown

. /etc/rc.subr

name="{service_name}"
rcvar={service_name}_enable

load_rc_config $name

: ${{{service_name}_enable:="NO"}}
: ${{{service_name}_dir:="{remote_dir}"}}
: ${{{service_name}_host:="{host}"}}
: ${{{service_name}_port:="{port}"}}
: ${{{service_name}_user:="{run_user}"}}
: ${{{service_name}_api_key:=""}}
: ${{{service_name}_admin_token:=""}}
: ${{{service_name}_console_token_secret:=""}}

pidfile="/var/run/${{name}}.pid"
command="/usr/sbin/daemon"
start_precmd="{service_name}_prestart"

{service_name}_prestart()
{{
    if [ ! -x "${{{service_name}_dir}}/scripts/run_poseidon.sh" ]; then
        echo "Missing launcher script: ${{{service_name}_dir}}/scripts/run_poseidon.sh"
        return 1
    fi

    export POSEIDON_HOST="${{{service_name}_host}}"
    export POSEIDON_PORT="${{{service_name}_port}}"
    export POSEIDON_API_KEY="${{{service_name}_api_key}}"
    export POSEIDON_ADMIN_TOKEN="${{{service_name}_admin_token}}"
    export POSEIDON_CONSOLE_TOKEN_SECRET="${{{service_name}_console_token_secret}}"
}}

command_args="-f -P ${{pidfile}} -u ${{{service_name}_user}} -o /var/log/${{name}}.log ${{{service_name}_dir}}/scripts/run_poseidon.sh"

run_rc_command "$1"
"""


RUNNER_SCRIPT_TEMPLATE = """#!/bin/sh
set -eu
cd "{remote_dir}"
exec .venv/bin/uvicorn app.main:app --host "${{POSEIDON_HOST:-{host}}}" --port "${{POSEIDON_PORT:-{port}}}"
"""