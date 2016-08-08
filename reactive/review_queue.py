import configparser
import os
import shutil
import subprocess
import tempfile

from charmhelpers.core.hookenv import charm_dir
from charmhelpers.core.hookenv import close_port
from charmhelpers.core.hookenv import config
from charmhelpers.core.hookenv import local_unit
from charmhelpers.core.hookenv import log
from charmhelpers.core.hookenv import open_port
from charmhelpers.core.hookenv import status_set
from charmhelpers.core.unitdata import kv

from charmhelpers.core.host import chownr
from charmhelpers.core.host import service_restart
from charmhelpers.core.host import service_running
from charmhelpers.core.host import service_stop
from charmhelpers.core.host import init_is_systemd

from charmhelpers.fetch import install_remote

from charms.reactive import hook
from charms.reactive import when
from charms.reactive import when_not
from charms.reactive import set_state
from charms.reactive import remove_state


config = config()
kvdb = kv()

DB_NAME = 'reviewqueue'
DB_ROLE = 'reviewqueue'

APP_DIR = '/opt/reviewqueue'
APP_INI_SRC = os.path.join(APP_DIR, 'production.ini')
APP_INI_DEST = '/etc/reviewqueue.ini'
APP_USER = 'ubuntu'
APP_GROUP = 'ubuntu'

SERVICE = 'reviewqueue'
TASK_SERVICE = 'reviewqueue-tasks'

UPSTART_FILE = '{}.conf'.format(SERVICE)
UPSTART_SRC = os.path.join(charm_dir(), 'files', 'upstart', UPSTART_FILE)
UPSTART_DEST = os.path.join('/etc/init', UPSTART_FILE)

UPSTART_TASK_FILE = '{}.conf'.format(TASK_SERVICE)
UPSTART_TASK_SRC = os.path.join(charm_dir(), 'files', 'upstart',
                                UPSTART_TASK_FILE)
UPSTART_TASK_DEST = os.path.join('/etc/init', UPSTART_TASK_FILE)

SYSTEMD_FILE = '{}.service'.format(SERVICE)
SYSTEMD_SRC = os.path.join(charm_dir(), 'files', 'systemd', SYSTEMD_FILE)
SYSTEMD_DEST = os.path.join('/etc/systemd/system', SYSTEMD_FILE)

SYSTEMD_TASK_FILE = '{}.service'.format(TASK_SERVICE)
SYSTEMD_TASK_SRC = os.path.join(charm_dir(), 'files', 'systemd',
                                SYSTEMD_TASK_FILE)
SYSTEMD_TASK_DEST = os.path.join('/etc/systemd/system', SYSTEMD_TASK_FILE)

LP_CREDS_FILE = 'lp-creds'
LP_CREDS_SRC = os.path.join(charm_dir(), 'files', LP_CREDS_FILE)
LP_CREDS_DEST = os.path.join(APP_DIR, LP_CREDS_FILE)


# List of app .ini keys that map to charm config.yaml keys
CFG_INI_KEYS = [
    'port',
    'base_url',
    'charmstore.api.url',
    'launchpad.api.url',
    'testing.timeout',
    'testing.substrates',
    'testing.default_substrates',
    'testing.jenkins_url',
    'testing.jenkins_token',
    'sendgrid.api_key',
    'mail.default_sender',
]

# Map ini key to the ini section it goes in. For ini keys not
# listed here, default section is 'app:main'
INI_SECTIONS = {
    'port': 'server:main',
}


@hook('upgrade-charm')
def upgrade_charm():
    # force update / reinstall
    install_review_queue()


@when('config.changed.repo')
def install_review_queue():
    status_set('maintenance', 'Installing Review Queue')

    with tempfile.TemporaryDirectory() as tmp_dir:
        install_dir = install_remote(config['repo'], dest=tmp_dir)
        contents = os.listdir(install_dir)
        if install_dir == tmp_dir and len(contents) == 1:
            # unlike the git handler, the archive handler just returns tmp_dir
            # even if the archive contents are nested in a folder as they
            # should be, so we have to normalize for that here
            install_dir = os.path.join(install_dir, contents[0])
        shutil.rmtree(APP_DIR, ignore_errors=True)
        log('Moving app source from {} to {}'.format(
            install_dir, APP_DIR))
        shutil.move(install_dir, APP_DIR)
    subprocess.check_call('make .venv'.split(), cwd=APP_DIR)
    if init_is_systemd():
        shutil.copyfile(SYSTEMD_SRC, SYSTEMD_DEST)
        shutil.copyfile(SYSTEMD_TASK_SRC, SYSTEMD_TASK_DEST)
    else:
        shutil.copyfile(UPSTART_SRC, UPSTART_DEST)
        shutil.copyfile(UPSTART_TASK_SRC, UPSTART_TASK_DEST)
    shutil.copyfile(LP_CREDS_SRC, LP_CREDS_DEST)
    shutil.copyfile(APP_INI_SRC, APP_INI_DEST)
    chownr(APP_DIR, APP_USER, APP_GROUP)

    set_state('reviewqueue.installed')
    change_config()
    update_db()
    update_amqp()


@when('config.changed', 'reviewqueue.installed')
@when_not('config.changed.repo')  # handled by install_review_queue instead
def change_config():
    update_ini({key: config[key.replace('.', '_')] for key in CFG_INI_KEYS})


@when('website.available')
def configure_website(http):
    http.configure(config['port'])
    # manual set on the relation because haproxy extends the interface protocol
    http.conversation().set_remote('service_name', 'review-queue')


@when('amqp.connected')
def setup_amqp(amqp):
    amqp.request_access(
        username='reviewqueue',
        vhost='reviewqueue')


@when('amqp.available')
def configure_amqp(amqp):
    amqp_uri = 'amqp://{}:{}@{}:{}/{}'.format(
        amqp.username(),
        amqp.password(),
        amqp.private_address(),
        '5672',  # amqp.port() not available?
        amqp.vhost(),
    )

    if kvdb.get('amqp_uri') != amqp_uri:
        kvdb.set('amqp_uri', amqp_uri)
        update_amqp()


def update_amqp():
    amqp_uri = kvdb.get('amqp_uri')
    if amqp_uri:
        update_ini({
            'broker': amqp_uri,
            'backend': 'rpc://',
        }, section='celery')


@when_not('amqp.available')
def stop_task_service():
    kvdb.set('amqp_uri', None)
    if service_running(TASK_SERVICE):
        service_stop(TASK_SERVICE)


@when('db.database.available')
def configure_db(db):
    db_uri = db.master.uri

    if kvdb.get('db_uri') != db_uri or not service_running(SERVICE):
        kvdb.set('db_uri', db_uri)
        update_db()


def update_db():
    db_uri = kvdb.get('db_uri')
    if db_uri:
        update_ini({
            'sqlalchemy.url': db_uri,
        })

        # initialize the DB
        subprocess.check_call(['/opt/reviewqueue/.venv/bin/initialize_db',
                               '/etc/reviewqueue.ini'])


@when_not('db.database.available')
def stop_web_service():
    kvdb.set('db_uri', None)
    if service_running(SERVICE):
        service_stop(SERVICE)
    status_set('waiting', 'Waiting for database')


@when('nrpe-external-master.available')
def setup_nagios(nagios):
    nagios.add_check([
        '/usr/lib/nagios/plugins/check_http',
        '-I', '127.0.0.1',
        '-p', str(config['port']),
        '-u', '/reviews',
        '-e', " 200 OK"],
        name="check_http",
        description="Verify Review Queue website is responding",
        context=config["nagios_context"],
        unit=local_unit(),
    )


@when('db.database.available', 'reviewqueue.restart')
def restart_web_service(db):
    started = service_restart(SERVICE)
    if started:
        status_set('active', 'Serving on port {port}'.format(**config))
    else:
        status_set('blocked', 'Service failed to start')
    remove_state('reviewqueue.restart')
    return started


@when('amqp.available', 'reviewqueue.restart')
def restart_task_service(amqp):
    service_restart(TASK_SERVICE)


def update_ini(kv_pairs, section=None):
    ini_changed = False

    ini = configparser.RawConfigParser()
    ini.read(APP_INI_DEST)

    for k, v in kv_pairs.items():
        this_section = INI_SECTIONS.get(k, section) or 'app:main'
        curr_val = ini.get(this_section, k)
        if curr_val != v:
            ini_changed = True
            log('[{}] {} = {}'.format(this_section, k, v))
            ini.set(this_section, k, v)

    if ini_changed:
        with open(APP_INI_DEST, 'w') as f:
            ini.write(f)
        set_state('reviewqueue.restart')


@when('config.changed.port', 'reviewqueue.installed')
def update_port():
    open_port(config['port'])
    if config.previous('port'):
        close_port(config.previous('port'))
