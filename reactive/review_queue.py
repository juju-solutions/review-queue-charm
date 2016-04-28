import configparser
import os
import shutil
import subprocess

from charmhelpers.core.hookenv import charm_dir
from charmhelpers.core.hookenv import close_port
from charmhelpers.core.hookenv import config
from charmhelpers.core.unitdata import kv
from charmhelpers.core.hookenv import log
from charmhelpers.core.hookenv import open_port
from charmhelpers.core.hookenv import status_set

from charmhelpers.core.host import chownr
from charmhelpers.core.host import service_restart
from charmhelpers.core.host import service_running
from charmhelpers.core.host import service_stop

from charmhelpers.fetch import install_remote

from charms.reactive import when
from charms.reactive import when_not
from charms.reactive import when_file_changed
from charms.reactive import relations
from charms.reactive import set_state
# from charms.reactive import hook


config = config()
db = kv()

DB_NAME = 'reviewqueue'
DB_ROLE = 'reviewqueue'

APP_DIR = '/opt/reviewqueue'
APP_INI_SRC = os.path.join(APP_DIR, 'production.ini')
APP_INI_DEST = '/etc/reviewqueue.ini'
APP_USER = 'ubuntu'
APP_GROUP = 'ubuntu'

SERVICE = 'reviewqueue'

UPSTART_FILE = '{}.conf'.format(SERVICE)
UPSTART_SRC = os.path.join(charm_dir(), 'files', UPSTART_FILE)
UPSTART_DEST = os.path.join('/etc/init', UPSTART_FILE)


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
    'sendgrid.from_email',
]

# Map ini key to the ini section it goes in. For ini keys not
# listed here, default section is 'app:main'
INI_SECTIONS = {
    'port': 'server:main',
}


@when('config.changed.repo')
def install_review_queue():
    status_set('maintenance', 'Installing Review Queue')

    tmp_dir = install_remote(config['repo'], dest='/tmp', depth='1')
    shutil.rmtree(APP_DIR, ignore_errors=True)
    log('Moving app source from {} to {}'.format(
        tmp_dir, APP_DIR))
    shutil.move(tmp_dir, APP_DIR)
    subprocess.check_call('make .venv'.split(), cwd=APP_DIR)
    shutil.copyfile(UPSTART_SRC, UPSTART_DEST)
    chownr(APP_DIR, APP_USER, APP_GROUP)

    set_state('review-queue.installed')


@when('config.changed', 'review-queue.installed')
def change_config():
    changes = []

    for ini_key in CFG_INI_KEYS:
        cfg_key = ini_key.replace('.', '_')
        if config.changed(cfg_key):
            changes.append((ini_key, config[cfg_key]))

    if changes:
        update_ini(changes)
        for change in changes:
            after_config_change(change[0])


@when('website.available')
def configure_website(http):
    http.configure(config['port'])


@when('db.database.available')
def render_ini(pgsql):
    db_uri = 'postgresql://{}:{}@{}:{}/{}'.format(
        pgsql.user(),
        pgsql.password(),
        pgsql.host(),
        pgsql.port(),
        pgsql.database(),
    )

    update_ini([
        ('sqlalchemy.url', db_uri),
    ])


@when_not('db.database.available')
def stop_service():
    if service_running(SERVICE):
        service_stop(SERVICE)
    status_set('waiting', 'Waiting for database')


@when('db.database.available')
@when_file_changed(APP_INI_DEST)
def restart_service():
    service_restart(SERVICE)
    status_set('active', 'Serving on port {port}'.format(**config))


def update_ini(kv_pairs):
    ini_changed = False

    ini = configparser.RawConfigParser()
    ini.read(APP_INI_SRC)

    for k, v in kv_pairs:
        section = INI_SECTIONS.get(k, 'app:main')
        curr_val = ini.get(section, k)
        if curr_val != v:
            ini_changed = True
            ini.set(section, k, v)

    if ini_changed:
        with open(APP_INI_SRC, 'w') as f:
            ini.write(f)


def after_config_change(config_key):
    if config_key == 'port':
        open_port(config['port'])
        if config.previous('port'):
            close_port(config.previous('port'))
        http = relations.RelationBase.from_state('website.available')
        if http:
            http.configure(config['port'])
