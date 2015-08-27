#!/usr/bin/python
import grp
import hashlib
import os
import pwd
import shutil
import subprocess
import sys

from charmhelpers.core.hookenv import (
    Hooks,
    UnregisteredHookError,
    config,
    remote_unit,
    relation_get,
    relation_set,
    relation_ids,
    unit_get,
    open_port,
    log,
    DEBUG,
    INFO,
)
from charmhelpers.fetch import (
    apt_install,
    apt_update,
)
from charmhelpers.core.host import (
    service_start,
    service_stop,
)
from charmhelpers.payload.execd import execd_preinstall
from jenkins_utils import (
    JENKINS_HOME,
    JENKINS_USERS,
    TEMPLATES_DIR,
    add_node,
    del_node,
    setup_source,
    install_from_bundle,
    install_jenkins_plugins,
)

hooks = Hooks()


@hooks.hook('install')
def install():
    execd_preinstall('hooks/install.d')
    if config('release') == 'bundle':
        install_from_bundle()
    else:
        # Only setup the source if jenkins is not already installed i.e. makes
        # the config 'release' immutable so you can't change source once
        # deployed.
        setup_source(config('release'))
    config_changed()
    open_port(8080)


@hooks.hook('config-changed')
def config_changed():
    apt_update()
    # Re-run whenever called to pickup any updates
    log("Installing/upgrading jenkins.", level=DEBUG)
    apt_install(['jenkins', 'default-jre-headless', 'pwgen'], fatal=True)

    # Always run - even if config has not changed, its safe
    log("Configuring user for jenkins.", level=DEBUG)
    # Check to see if password provided
    admin_passwd = config('password')
    if not admin_passwd:
        # Generate a random one for security. User can then override using juju
        # set.
        admin_passwd = subprocess.check_output(['pwgen', '-N1', '15'])
        admin_passwd = admin_passwd.strip()

    passwd_file = os.path.join(JENKINS_HOME, '.admin_password')
    with open(passwd_file, 'w+') as fd:
        fd.write(admin_passwd)

    os.chmod(passwd_file, 0600)

    jenkins_uid = pwd.getpwnam('jenkins').pw_uid
    jenkins_gid = grp.getgrnam('jenkins').gr_gid
    nogroup_gid = grp.getgrnam('nogroup').gr_gid

    # Generate Salt and Hash Password for Jenkins
    salt = subprocess.check_output(['pwgen', '-N1', '6']).strip()
    csum = hashlib.sha256("%s{%s}" % (admin_passwd, salt)).hexdigest()
    salty_password = "%s:%s" % (salt, csum)

    admin_username = config('username')
    admin_user_home = os.path.join(JENKINS_USERS, admin_username)
    if not os.path.isdir(admin_user_home):
        os.makedirs(admin_user_home, 0o0700)
        os.chown(JENKINS_USERS, jenkins_uid, nogroup_gid)
        os.chown(admin_user_home, jenkins_uid, nogroup_gid)

    # NOTE: overwriting will destroy any data added by jenkins or via the ui
    admin_user_config = os.path.join(admin_user_home, 'config.xml')
    with open(os.path.join(TEMPLATES_DIR, 'user-config.xml')) as src_fd:
        with open(admin_user_config, 'w') as dst_fd:
            lines = src_fd.readlines()
            for line in lines:
                kvs = {'__USERNAME__': admin_username,
                       '__PASSWORD__': salty_password}

                for key, val in kvs.iteritems():
                    if key in line:
                        line = line.replace(key, val)

                dst_fd.write(line)
                os.chown(admin_user_config, jenkins_uid, nogroup_gid)

    # Only run on first invocation otherwise we blast
    # any configuration changes made
    jenkins_bootstrap_flag = '/var/lib/jenkins/config.bootstrapped'
    if not os.path.exists(jenkins_bootstrap_flag):
        log("Bootstrapping secure initial configuration in Jenkins.",
            level=DEBUG)
        src = os.path.join(TEMPLATES_DIR, 'jenkins-config.xml')
        dst = os.path.join(JENKINS_HOME, 'config.xml')
        shutil.copy(src, dst)
        os.chown(dst, jenkins_uid, nogroup_gid)
        # Touch
        with open(jenkins_bootstrap_flag, 'w'):
            pass

    log("Stopping jenkins for plugin update(s)", level=DEBUG)
    service_stop('jenkins')
    install_jenkins_plugins(jenkins_uid, jenkins_gid)
    log("Starting jenkins to pickup configuration changes", level=DEBUG)
    service_start('jenkins')

    apt_install(['python-jenkins'], fatal=True)
    tools = config('tools')
    if tools:
        log("Installing tools.", level=DEBUG)
        apt_install(tools.split(), fatal=True)


@hooks.hook('start')
def start():
    service_start('jenkins')


@hooks.hook('stop')
def stop():
    service_stop('jenkins')


@hooks.hook('upgrade-charm')
def upgrade_charm():
    log("Upgrading charm.", level=DEBUG)
    config_changed()


@hooks.hook('master-relation-joined')
def master_relation_joined():
    HOSTNAME = unit_get('private-address')
    log("Setting url relation to http://%s:8080" % (HOSTNAME), level=DEBUG)
    relation_set(url="http://%s:8080" % (HOSTNAME))


@hooks.hook('master-relation-changed')
def master_relation_changed():
    PASSWORD = config('password')
    if PASSWORD:
        with open('/var/lib/jenkins/.admin_password', 'r') as fd:
            PASSWORD = fd.read()

    required_settings = ['executors', 'labels', 'slavehost']
    settings = relation_get()
    missing = [s for s in required_settings if s not in settings]
    if missing:
        log("Not all required relation settings received yet (missing=%s) - "
            "skipping" % (', '.join(missing)), level=INFO)
        return

    slavehost = settings['slavehost']
    executors = settings['executors']
    labels = settings['labels']

    # Double check to see if this has happened yet
    if "x%s" % (slavehost) == "x":
        log("Slave host not yet defined - skipping", level=INFO)
        return

    log("Adding slave with hostname %s." % (slavehost), level=DEBUG)
    add_node(slavehost, executors, labels, config('username'), PASSWORD)
    log("Node slave %s added." % (slavehost), level=DEBUG)


@hooks.hook('master-relation-departed')
def master_relation_departed():
    # Slave hostname is derived from unit name so
    # this is pretty safe
    slavehost = remote_unit()
    log("Deleting slave with hostname %s." % (slavehost), level=DEBUG)
    del_node(slavehost, config('username'), config('password'))


@hooks.hook('master-relation-broken')
def master_relation_broken():
    password = config('password')
    if not password:
        passwd_file = os.path.join(JENKINS_HOME, '.admin_password')
        with open(passwd_file, 'r') as fd:
            password = fd.read()

    for member in relation_ids():
        member = member.replace('/', '-')
        log("Removing node %s from Jenkins master." % (member), level=DEBUG)
        del_node(member, config('username'), password)


@hooks.hook('website-relation-joined')
def website_relation_joined():
    hostname = unit_get('private-address')
    log("Setting website URL to %s:8080" % (hostname), level=DEBUG)
    relation_set(port=8080, hostname=hostname)


if __name__ == '__main__':
    try:
        hooks.execute(sys.argv)
    except UnregisteredHookError as e:
        log('Unknown hook {} - skipping.'.format(e), level=INFO)
