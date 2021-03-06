# test mod_md acme terms-of-service handling

import copy
import json
import pytest
import re
import os
import shutil
import socket
import subprocess
import sys
import time
import OpenSSL

from datetime import datetime
from datetime import tzinfo
from datetime import timedelta
from ConfigParser import SafeConfigParser
from httplib import HTTPConnection
from shutil import copyfile
from urlparse import urlparse

SEC_PER_DAY = 24 * 60 * 60

class TestEnv:

    @classmethod
    def _init_base( cls ) :
        cls.ACME_URL = None
        cls.STORE_DIR = None

        cls.config = SafeConfigParser()
        cls.config.read('test.ini')
        cls.PREFIX = cls.config.get('global', 'prefix')

        cls.GEN_DIR   = cls.config.get('global', 'gen_dir')

        cls.WEBROOT   = cls.config.get('global', 'server_dir')
        cls.TESTROOT  = os.path.join(cls.WEBROOT, '..', '..')
        
        cls.APACHECTL = os.path.join(cls.PREFIX, 'bin', 'apachectl')
        cls.APXS = os.path.join(cls.PREFIX, 'bin', 'apxs')
        cls.ERROR_LOG = os.path.join(cls.WEBROOT, "logs", "error_log")
        cls.APACHE_CONF_DIR = os.path.join(cls.WEBROOT, "conf")
        cls.APACHE_SSL_DIR = os.path.join(cls.APACHE_CONF_DIR, "ssl")
        cls.APACHE_CONF = os.path.join(cls.APACHE_CONF_DIR, "httpd.conf")
        cls.APACHE_TEST_CONF = os.path.join(cls.APACHE_CONF_DIR, "test.conf")
        cls.APACHE_CONF_SRC = "data"
        cls.APACHE_HTDOCS_DIR = os.path.join(cls.WEBROOT, "htdocs")

        cls.HTTP_PORT = cls.config.get('global', 'http_port')
        cls.HTTPS_PORT = cls.config.get('global', 'https_port')
        cls.HTTP_PROXY_PORT = cls.config.get('global', 'http_proxy_port')
        cls.HTTPD_HOST = "localhost"
        cls.HTTPD_URL = "http://" + cls.HTTPD_HOST + ":" + cls.HTTP_PORT
        cls.HTTPD_URL_SSL = "https://" + cls.HTTPD_HOST + ":" + cls.HTTPS_PORT
        cls.HTTPD_PROXY_URL = "http://" + cls.HTTPD_HOST + ":" + cls.HTTP_PROXY_PORT
        cls.HTTPD_CHECK_URL = cls.HTTPD_PROXY_URL 

        cls.A2MD      = cls.config.get('global', 'a2md_bin')
        cls.CURL      = cls.config.get('global', 'curl_bin')
        cls.OPENSSL   = cls.config.get('global', 'openssl_bin')

        cls.MD_S_UNKNOWN = 0
        cls.MD_S_INCOMPLETE = 1
        cls.MD_S_COMPLETE = 2
        cls.MD_S_EXPIRED = 3
        cls.MD_S_ERROR = 4

        cls.EMPTY_JOUT = { 'status' : 0, 'output' : [] }

        cls.ACME_SERVER_DOWN = False
        cls.ACME_SERVER_OK = False

        cls.DOMAIN_SUFFIX = "%d.org" % time.time()

        cls.set_store_dir_default()
        cls.set_acme('acmev2')
        cls.clear_store()
        cls.install_test_conf()

    @classmethod
    def set_acme( cls, acme_section ) :
        cls.ACME_URL_DEFAULT  = cls.config.get(acme_section, 'url_default')
        cls.ACME_URL  = cls.config.get(acme_section, 'url')
        cls.ACME_TOS  = cls.config.get(acme_section, 'tos')
        cls.ACME_TOS2 = cls.config.get(acme_section, 'tos2')
        cls.BOULDER_DIR = cls.config.get(acme_section, 'boulder_dir')
        if cls.STORE_DIR:
            cls.a2md_stdargs([cls.A2MD, "-a", cls.ACME_URL, "-d", cls.STORE_DIR, "-j" ])
            cls.a2md_rawargs([cls.A2MD, "-a", cls.ACME_URL, "-d", cls.STORE_DIR ])

    @classmethod
    def init( cls ) :
        cls._init_base()

    @classmethod
    def initv1( cls ) :
        cls._init_base()
        cls.set_acme('acmev1')

    @classmethod
    def initv2( cls ) :
        cls._init_base()

    @classmethod
    def set_store_dir( cls, dir ) :
        cls.STORE_DIR = os.path.join(cls.WEBROOT, dir)
        if cls.ACME_URL:
            cls.a2md_stdargs([cls.A2MD, "-a", cls.ACME_URL, "-d", cls.STORE_DIR, "-j" ])
            cls.a2md_rawargs([cls.A2MD, "-a", cls.ACME_URL, "-d", cls.STORE_DIR ])

    @classmethod
    def set_store_dir_default( cls ) :
        dir = "md"
        if cls.httpd_is_at_least("2.5.0"):
            dir = os.path.join("state", dir)
        cls.set_store_dir(dir)

    @classmethod
    def get_method_domain( cls, method ) :
        return "%s-%s" % (re.sub(r'[_]', '-', method.__name__), TestEnv.DOMAIN_SUFFIX)

    # --------- cmd execution ---------

    _a2md_args = []
    _a2md_args_raw = []
    
    @classmethod
    def run( cls, args, input=None ) :
        #print "execute: ", " ".join(args)
        p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (output, errput) = p.communicate(input)
        rv = p.wait()
        try:
            jout = json.loads(output)
        except:
            jout = None
            print "stderr: ", errput
            print "stdout: ", output
        return { 
            "rv": rv, 
            "stdout": output, 
            "stderr": errput,
            "jout" : jout 
        }

    @classmethod
    def a2md_stdargs( cls, args ) :
        cls._a2md_args = [] + args 

    @classmethod
    def a2md_rawargs( cls, args ) :
        cls._a2md_args_raw = [] + args
         
    @classmethod
    def a2md( cls, args, raw=False ) :
        preargs = cls._a2md_args
        if raw :
            preargs = cls._a2md_args_raw
        return cls.run( preargs + args )

    @classmethod
    def curl( cls, args ) :
        return cls.run( [ cls.CURL ] + args )

    # --------- HTTP ---------

    @classmethod
    def is_live( cls, url, timeout ) :
        server = urlparse(url)
        try_until = time.time() + timeout
        print("checking reachability of %s" % url)
        while time.time() < try_until:
            try:
                c = HTTPConnection(server.hostname, server.port, timeout=timeout)
                c.request('HEAD', server.path)
                resp = c.getresponse()
                c.close()
                return True
            except IOError:
                print "connect error:", sys.exc_info()[0]
                time.sleep(.1)
            except:
                print "Unexpected error:", sys.exc_info()[0]
                time.sleep(.1)
        print "Unable to contact server after %d sec" % timeout
        return False

    @classmethod
    def is_dead( cls, url, timeout ) :
        server = urlparse(url)
        try_until = time.time() + timeout
        print("checking reachability of %s" % url)
        while time.time() < try_until:
            try:
                c = HTTPConnection(server.hostname, server.port, timeout=timeout)
                c.request('HEAD', server.path)
                resp = c.getresponse()
                c.close()
                time.sleep(.1)
            except IOError:
                return True
            except:
                return True
        print "Server still responding after %d sec" % timeout
        return False

    @classmethod
    def get_json( cls, url, timeout ) :
        data = cls.get_plain( url, timeout )
        if data:
            return json.loads(data)
        return None

    @classmethod
    def get_plain( cls, url, timeout ) :
        server = urlparse(url)
        try_until = time.time() + timeout
        while time.time() < try_until:
            try:
                c = HTTPConnection(server.hostname, server.port, timeout=timeout)
                c.request('GET', server.path)
                resp = c.getresponse()
                data = resp.read()
                c.close()
                return data
            except IOError:
                print "connect error:", sys.exc_info()[0]
                time.sleep(.1)
            except:
                print "Unexpected error:", sys.exc_info()[0]
        print "Unable to contact server after %d sec" % timeout
        return None

    @classmethod
    def check_acme( cls ) :
        if cls.ACME_SERVER_OK:
            return True
        if cls.ACME_SERVER_DOWN:
            pytest.skip(msg="ACME server not running")
            return False
        if cls.is_live(cls.ACME_URL, 0.5):
            cls.ACME_SERVER_OK = True
            return True
        else:
            cls.ACME_SERVER_DOWN = True
            pytest.fail(msg="ACME server not running", pytrace=False)
            return False

    @classmethod
    def get_httpd_version( cls ) :
        p = subprocess.Popen([ cls.APXS, "-q", "HTTPD_VERSION" ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (output, errput) = p.communicate()
        rv = p.wait()
        if rv != 0:
            return "unknown"
        return output.strip()
        
    @classmethod
    def _versiontuple( cls, v ):
        return tuple(map(int, (v.split("."))))
    
    @classmethod
    def httpd_is_at_least( cls, minv ) :
        hv = cls._versiontuple(cls.get_httpd_version())
        return hv >= cls._versiontuple(minv)

    # --------- access local store ---------

    @classmethod
    def purge_store( cls ) : 
        print("purge store dir: %s" % TestEnv.STORE_DIR)
        assert len(TestEnv.STORE_DIR) > 1
        if os.path.exists(TestEnv.STORE_DIR):
            shutil.rmtree(TestEnv.STORE_DIR, ignore_errors=False)
        os.makedirs(TestEnv.STORE_DIR)

    @classmethod
    def clear_store( cls ) : 
        print("clear store dir: %s" % TestEnv.STORE_DIR)
        assert len(TestEnv.STORE_DIR) > 1
        if not os.path.exists(TestEnv.STORE_DIR):
            os.makedirs(TestEnv.STORE_DIR)
        for dir in [ "challenges", "tmp", "archive", "domains", "accounts", "staging" ]:
            shutil.rmtree(os.path.join(TestEnv.STORE_DIR, dir), ignore_errors=True)

    @classmethod
    def authz_save( cls, name, content ) :
        dir = os.path.join(TestEnv.STORE_DIR, 'staging', name)
        os.makedirs(dir)
        open( os.path.join( dir, 'authz.json'), "w" ).write(content)

    @classmethod
    def path_store_json( cls ) : 
        return os.path.join(TestEnv.STORE_DIR, 'md_store.json')

    @classmethod
    def path_account( cls, acct ) : 
        return os.path.join(TestEnv.STORE_DIR, 'accounts', acct, 'account.json')

    @classmethod
    def path_account_key( cls, acct ) : 
        return os.path.join(TestEnv.STORE_DIR, 'accounts', acct, 'account.pem')

    @classmethod
    def store_domains( cls ) :
        return os.path.join(TestEnv.STORE_DIR, 'domains')

    @classmethod
    def store_archives( cls ) :
        return os.path.join(TestEnv.STORE_DIR, 'archive')

    @classmethod
    def store_stagings( cls ) :
        return os.path.join(TestEnv.STORE_DIR, 'staging')

    @classmethod
    def store_challenges( cls ) :
        return os.path.join(TestEnv.STORE_DIR, 'challenges')
    
    @classmethod
    def store_domain_file( cls, domain, filename ) :
        return os.path.join(TestEnv.store_domains(), domain, filename)

    @classmethod
    def store_archived_file( cls, domain, version, filename ) :
        return os.path.join(TestEnv.store_archives(), "%s.%d" % (domain, version), filename)
     
    @classmethod
    def store_staged_file( cls, domain, filename ) :
        return os.path.join(TestEnv.store_stagings(), domain, filename)
     
    @classmethod
    def path_fallback_cert( cls, domain ) :
        return os.path.join(TestEnv.STORE_DIR, 'domains', domain, 'fallback-cert.pem')

    @classmethod
    def path_job( cls, domain ) :
        return os.path.join( TestEnv.STORE_DIR, 'staging', domain, 'job.json' )

    @classmethod
    def replace_store( cls, src):
        shutil.rmtree(TestEnv.STORE_DIR, ignore_errors=False)
        shutil.copytree(src, TestEnv.STORE_DIR)

    @classmethod
    def list_accounts( cls ) :
        return os.listdir( os.path.join( TestEnv.STORE_DIR, 'accounts' ) )
    
    @classmethod
    def check_md(cls, domain, dnsList=None, state=-1, ca=None, protocol=None, agreement=None, contacts=None):
        path = cls.store_domain_file(domain, 'md.json')
        with open( path ) as f:
            md = json.load(f)
        assert md
        if dnsList:
            assert md['domains'] == dnsList
        if state >= 0:
            assert md['state'] == state
        if ca:
            assert md['ca']['url'] == ca
        if protocol:
            assert md['ca']['proto'] == protocol
        if agreement:
            assert md['ca']['agreement'] == agreement
        if contacts:
            assert md['contacts'] == contacts


    @classmethod
    def check_md_complete(cls, domain):
        md = cls.get_md_status(domain)
        assert md
        assert md['state'] == TestEnv.MD_S_COMPLETE
        assert os.path.isfile( TestEnv.store_domain_file(domain, 'privkey.pem') )
        assert os.path.isfile(  TestEnv.store_domain_file(domain, 'pubcert.pem') )

    @classmethod
    def check_md_credentials(cls, domain, dnsList):
        # check private key, validate certificate, etc
        CertUtil.validate_privkey( cls.store_domain_file(domain, 'privkey.pem') )
        cert = CertUtil(  cls.store_domain_file(domain, 'pubcert.pem') )
        cert.validate_cert_matches_priv_key( cls.store_domain_file(domain, 'privkey.pem') )
        # check SANs and CN
        assert cert.get_cn() == domain
        # compare lists twice in opposite directions: SAN may not respect ordering
        sanList = cert.get_san_list()
        assert len(sanList) == len(dnsList)
        assert set(sanList).issubset(dnsList)
        assert set(dnsList).issubset(sanList)
        # check valid dates interval
        notBefore = cert.get_not_before()
        notAfter = cert.get_not_after()
        assert notBefore < datetime.now(notBefore.tzinfo)
        assert notAfter > datetime.now(notAfter.tzinfo)

    # --------- control apache ---------

    @classmethod
    def install_test_conf( cls, conf=None) :
        root_conf_src = os.path.join("conf", "httpd.conf")
        copyfile(root_conf_src, cls.APACHE_CONF)

        if conf is None:
            conf_src = os.path.join("conf", "test.conf")
        elif os.path.isabs(conf):
            conf_src = conf
        else:
            conf_src = os.path.join(cls.APACHE_CONF_SRC, conf + ".conf")
        copyfile(conf_src, cls.APACHE_TEST_CONF)

    @classmethod
    def apachectl( cls, cmd, conf=None, check_live=True ) :
        if conf:
            cls.install_test_conf(conf)
        args = [cls.APACHECTL, "-d", cls.WEBROOT, "-k", cmd]
        #print "execute: ", " ".join(args)
        cls.apachectl_stderr = ""
        p = subprocess.Popen(args, stderr=subprocess.PIPE)
        (output, cls.apachectl_stderr) = p.communicate()
        sys.stderr.write(cls.apachectl_stderr)
        rv = p.wait()
        if rv == 0:
            if check_live:
                rv = 0 if cls.is_live(cls.HTTPD_CHECK_URL, 10) else -1
            else:
                rv = 0 if cls.is_dead(cls.HTTPD_CHECK_URL, 10) else -1
                print ("waited for a apache.is_dead, rv=%d" % rv)
        return rv

    @classmethod
    def apache_restart( cls ) :
        return cls.apachectl( "graceful" )
        
    @classmethod
    def apache_start( cls ) :
        return cls.apachectl( "start" )

    @classmethod
    def apache_stop( cls ) :
        return cls.apachectl( "stop", check_live=False )

    @classmethod
    def apache_fail( cls ) :
        rv = cls.apachectl( "graceful", check_live=False )
        if rv != 0:
            print "check, if dead: " + cls.HTTPD_CHECK_URL
            return 0 if cls.is_dead(cls.HTTPD_CHECK_URL, 5) else -1
        return rv
        
    @classmethod
    def apache_err_reset( cls ):
        cls.apachectl_stderr = ""
        if os.path.isfile(cls.ERROR_LOG):
            os.remove(cls.ERROR_LOG)

    RE_MD_RESET = re.compile('.*\[md:info\].*initializing\.\.\.')
    RE_MD_ERROR = re.compile('.*\[md:error\].*')
    RE_MD_WARN  = re.compile('.*\[md:warn\].*')

    @classmethod
    def apache_err_count( cls ):
        ecount = 0
        wcount = 0
        
        if os.path.isfile(cls.ERROR_LOG):
            fin = open(cls.ERROR_LOG)
            for line in fin:
                m = cls.RE_MD_ERROR.match(line)
                if m:
                    ecount += 1
                    continue
                m = cls.RE_MD_WARN.match(line)
                if m:
                    wcount += 1
                    continue
                m = cls.RE_MD_RESET.match(line)
                if m:
                    ecount = 0
                    wcount = 0
        return (ecount, wcount)

    @classmethod
    def apache_err_total( cls ):
        ecount = 0
        wcount = 0
        
        if os.path.isfile(cls.ERROR_LOG):
            fin = open(cls.ERROR_LOG)
            for line in fin:
                m = cls.RE_MD_ERROR.match(line)
                if m:
                    ecount += 1
                    continue
                m = cls.RE_MD_WARN.match(line)
                if m:
                    wcount += 1
                    continue
        return (ecount, wcount)

    @classmethod
    def apache_err_scan( cls, regex ):
        if not os.path.isfile(cls.ERROR_LOG):
            return False
        fin = open(cls.ERROR_LOG)
        for line in fin:
            if regex.match(line):
                return True
        return False


    # --------- check utilities ---------

    @classmethod
    def check_json_contains(cls, actual, expected):
        # write all expected key:value bindings to a copy of the actual data ... 
        # ... assert it stays unchanged 
        testJson = copy.deepcopy(actual)
        testJson.update(expected)
        assert actual == testJson

    @classmethod
    def check_file_access(cls, path, expMask):
         actualMask = os.lstat(path).st_mode & 0777
         assert oct(actualMask) == oct(expMask)

    @classmethod
    def check_dir_empty(cls, path):
         assert os.listdir(path) == []

    @classmethod
    def getStatus(cls, domain, path, useHTTPS=True):
        result = cls.get_meta(domain, path, useHTTPS)
        return result['http_status']

    @classmethod
    def get_meta(cls, domain, path, useHTTPS=True):
        schema = "https" if useHTTPS else "http"
        port = cls.HTTPS_PORT if useHTTPS else cls.HTTP_PORT
        result = TestEnv.curl([ "-D", "-", "-k", "--resolve", ("%s:%s:127.0.0.1" % (domain, port)), 
                               ("%s://%s:%s%s" % (schema, domain, port, path)) ])
        assert result['rv'] == 0
        # read status
        m = re.match("HTTP/\\d(\\.\\d)? +(\\d\\d\\d) .*", result['stdout'])
        assert m
        result['http_status'] = int(m.group(2))
        # collect response headers
        h = {}
        for m in re.findall("^(\\S+): (.*)\r$", result['stdout'], re.M) :
            h[ m[0] ] = m[1]
        result['http_headers'] = h
        return result

    @classmethod
    def get_content(cls, domain, path, useHTTPS=True):
        schema = "https" if useHTTPS else "http"
        port = cls.HTTPS_PORT if useHTTPS else cls.HTTP_PORT
        result = TestEnv.curl([ "-sk", "--resolve", ("%s:%s:127.0.0.1" % (domain, port)), 
                               ("%s://%s:%s%s" % (schema, domain, port, path)) ])
        assert result['rv'] == 0
        return result['stdout']

    @classmethod
    def get_json_content(cls, domain, path, useHTTPS=True):
        schema = "https" if useHTTPS else "http"
        port = cls.HTTPS_PORT if useHTTPS else cls.HTTP_PORT
        result = TestEnv.curl([ "-k", "--resolve", ("%s:%s:127.0.0.1" % (domain, port)), 
                               ("%s://%s:%s%s" % (schema, domain, port, path)) ])
        assert result['rv'] == 0
        return result['jout'] if 'jout' in result else None

    @classmethod
    def get_certificate_status(cls, domain, timeout=60):
        stat = TestEnv.get_json_content(domain, "/.httpd/certificate-status")
        return stat

    @classmethod
    def get_md_status(cls, domain, timeout=60):
        stat = TestEnv.get_json_content("localhost", "/md-status/%s" % (domain))
        return stat

    @classmethod
    def await_completion(cls, names, must_renew=False, restart=True, timeout=60):
        try_until = time.time() + timeout
        renewals = {}
        while len(names) > 0:
            if time.time() >= try_until:
                return False
            for name in names:
                md = TestEnv.get_md_status(name, timeout)
                if md == None:
                    print "not managed by md: %s" % (name)
                    return False

                if 'renewal' in md:
                    renewal = md['renewal']
                    renewals[name] = True
                    if 'finished' in renewal and renewal['finished'] == True:
                        if (not must_renew) or (name in renewals):
                            names.remove(name)                        
                    
            if len(names) != 0:
                time.sleep(0.1)
        if restart:
            time.sleep(0.1)
            return cls.apache_restart() == 0
        return True

    @classmethod
    def is_renewing(cls, name, timeout=60):
        stat = TestEnv.get_certificate_status(name, timeout)
        return 'renewal' in stat

    @classmethod
    def await_renew_state(cls, names, timeout=60):
        try_until = time.time() + timeout
        while len(names) > 0:
            if time.time() >= try_until:
                return False
            allChanged = True
            for name in names:
                # check status in md.json
                md = TestEnv.a2md( [ "list", name ] )['jout']['output'][0]
                if 'renew' in md and md['renew'] == True:
                    names.remove(name)

            if len(names) != 0:
                time.sleep(0.1)
        return True

    @classmethod
    def await_error(cls, domain, timeout=60):
        try_until = time.time() + timeout
        while True:
            if time.time() >= try_until:
                return False
            md = cls.get_md_status(domain)
            if md:
                if md['state'] == TestEnv.MD_S_ERROR:
                    return md
                if 'renewal' in md and 'errors' in md['renewal'] and md['renewal']['errors'] > 0:
                    return md
            time.sleep(0.1)

    @classmethod
    def check_file_permissions( cls, domain ):
        md = cls.a2md([ "list", domain ])['jout']['output'][0]
        assert md
        acct = md['ca']['account']
        assert acct
        cls.check_file_access( cls.path_store_json(), 0600 )
        # domains
        cls.check_file_access( cls.store_domains(), 0700 )
        cls.check_file_access( os.path.join( cls.store_domains(), domain ), 0700 )
        cls.check_file_access( cls.store_domain_file( domain, 'privkey.pem' ), 0600 )
        cls.check_file_access( cls.store_domain_file( domain, 'pubcert.pem' ), 0600 )
        cls.check_file_access( cls.store_domain_file( domain, 'md.json' ), 0600 )
        # archive
        cls.check_file_access( cls.store_archived_file( domain, 1, 'md.json' ), 0600 )
        # accounts
        cls.check_file_access( os.path.join( cls.STORE_DIR, 'accounts' ), 0755 )
        cls.check_file_access( os.path.join( cls.STORE_DIR, 'accounts', acct ), 0755 )
        cls.check_file_access( cls.path_account( acct ), 0644 )
        cls.check_file_access( cls.path_account_key( acct ), 0644 )
        # staging
        cls.check_file_access( cls.store_stagings(), 0755 )

# -----------------------------------------------
# --
# --     dynamic httpd configuration
# --

class HttpdConf(object):
    # Utility class for creating Apache httpd test configurations

    def __init__(self, name="test.conf"):
        self.path = os.path.join(TestEnv.GEN_DIR, name)
        if os.path.isfile(self.path):
            os.remove(self.path)
        open(self.path, "a").write((
            "MDCertificateAuthority %s\n"
            "MDCertificateAgreement %s\n") % 
            (TestEnv.ACME_URL, 'accepted')
        );

    def clear(self):
        if os.path.isfile(self.path):
            os.remove(self.path)

    def _add_line(self, line):
        open(self.path, "a").write(line + "\n")

    def add_line(self, line):
        self._add_line(line)

    def add_drive_mode(self, mode):
        self._add_line("  MDDriveMode %s\n" % mode)

    def add_renew_window(self, window):
        self._add_line("  MDRenewWindow %s\n" % window)

    def add_private_key(self, keyType, keyParams):
        self._add_line("  MDPrivateKeys %s %s\n" % (keyType, " ".join(map(lambda p: str(p), keyParams))) )

    def add_admin(self, email):
        self._add_line("  ServerAdmin mailto:%s\n\n" % email)

    def add_md(self, dnsList):
        self._add_line("  MDomain %s\n\n" % " ".join(dnsList))

    def start_md(self, dnsList):
        self._add_line("  <MDomainSet %s>\n" % " ".join(dnsList))

    def end_md(self):
        self._add_line("  </MDomainSet>\n")

    def add_must_staple(self, mode):
        self._add_line("  MDMustStaple %s\n" % mode)

    def add_ca_challenges(self, type_list):
        self._add_line("  MDCAChallenges %s\n" % " ".join(type_list))

    def add_http_proxy(self, url):
        self._add_line("  MDHttpProxy %s\n" % url)

    def add_require_ssl(self, mode):
        self._add_line("  MDRequireHttps %s\n" % mode)

    def add_notify_cmd(self, cmd):
        self._add_line("  MDNotifyCmd %s\n" % cmd)

    def add_dns01_cmd(self, cmd):
        self._add_line("  MDChallengeDns01 %s\n" % cmd)

    def add_vhost(self, port, domain, aliasList=[], docRoot="htdocs"):
        self.start_vhost(port, domain, aliasList, docRoot)
        self.end_vhost()

    def start_vhost(self, port, domain, aliasList=[], docRoot="htdocs"):
        f = open(self.path, "a") 
        f.write("<VirtualHost *:%s>\n" % port)
        f.write("    ServerName %s\n" % domain)
        for alias in aliasList:
            f.write("    ServerAlias %s\n" % alias )
        f.write("    DocumentRoot %s\n\n" % docRoot)
        if TestEnv.HTTPS_PORT == port:
            f.write("    SSLEngine on\n")
                  
    def end_vhost(self):
        self._add_line("</VirtualHost>\n\n")

    def install(self):
        TestEnv.install_test_conf(self.path)

# -----------------------------------------------
# --
# --     certificate handling
# --

class CertUtil(object):
    # Utility class for inspecting certificates in test cases
    # Uses PyOpenSSL: https://pyopenssl.org/en/stable/index.html

    @classmethod
    def create_self_signed_cert( cls, nameList, validDays, serial=1000 ):
        domain = nameList[0]
        certFilePath =  TestEnv.store_domain_file(domain, 'pubcert.pem')
        keyFilePath = TestEnv.store_domain_file(domain, 'privkey.pem')

        ddir = os.path.join(TestEnv.store_domains(), domain)
        if not os.path.exists(ddir):
            os.makedirs(ddir)

        # create a key pair
        if os.path.exists(keyFilePath):
            key_buffer = open(keyFilePath, 'rt').read()
            k = OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM, key_buffer)
        else:
            k = OpenSSL.crypto.PKey()
            k.generate_key(OpenSSL.crypto.TYPE_RSA, 1024)

        # create a self-signed cert
        cert = OpenSSL.crypto.X509()
        cert.get_subject().C = "DE"
        cert.get_subject().ST = "NRW"
        cert.get_subject().L = "Muenster"
        cert.get_subject().O = "greenbytes GmbH"
        cert.get_subject().CN = domain
        cert.set_serial_number(serial)
        cert.gmtime_adj_notBefore( validDays["notBefore"] * SEC_PER_DAY)
        cert.gmtime_adj_notAfter( validDays["notAfter"] * SEC_PER_DAY)
        cert.set_issuer(cert.get_subject())

        cert.add_extensions([ OpenSSL.crypto.X509Extension(
            b"subjectAltName", False, ", ".join( map(lambda n: "DNS:" + n, nameList) )
        ) ])
        cert.set_pubkey(k)
        cert.sign(k, 'sha1')

        open(certFilePath, "wt").write(
            OpenSSL.crypto.dump_certificate(OpenSSL.crypto.FILETYPE_PEM, cert))
        open(keyFilePath, "wt").write(
            OpenSSL.crypto.dump_privatekey(OpenSSL.crypto.FILETYPE_PEM, k))

    @classmethod
    def load_server_cert( cls, hostIP, hostPort, hostName ):
        ctx = OpenSSL.SSL.Context(OpenSSL.SSL.SSLv23_METHOD)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        connection = OpenSSL.SSL.Connection(ctx, s)
        connection.connect((hostIP, int(hostPort)))
        connection.setblocking(1)
        connection.set_tlsext_host_name(hostName)
        connection.do_handshake()
        peer_cert = connection.get_peer_certificate()
        return CertUtil( None, cert=peer_cert )


    def __init__(self, cert_path, cert=None):
        if cert_path is not None:
            self.cert_path = cert_path
            # load certificate and private key
            if cert_path.startswith("http"):
                cert_data = TestEnv.get_plain(cert_path, 1)
            else:
                cert_data = CertUtil._load_binary_file(cert_path)

            for file_type in (OpenSSL.crypto.FILETYPE_PEM, OpenSSL.crypto.FILETYPE_ASN1):
                try:
                    self.cert = OpenSSL.crypto.load_certificate(file_type, cert_data)
                except Exception as error:
                    self.error = error
        if cert is not None:
            self.cert = cert

        if self.cert is None:
            raise self.error

    def get_issuer(self):
        return self.cert.get_issuer()

    def get_serial(self):
        return ("%lx" % (self.cert.get_serial_number())).upper()

    def get_not_before(self):
        tsp = self.cert.get_notBefore()
        return self._parse_tsp(tsp)

    def get_not_after(self):
        tsp = self.cert.get_notAfter()
        return self._parse_tsp(tsp)

    def get_cn(self):
        return self.cert.get_subject().CN

    def get_key_length(self):
        return self.cert.get_pubkey().bits()

    def get_san_list(self):
        text = OpenSSL.crypto.dump_certificate(OpenSSL.crypto.FILETYPE_TEXT, self.cert).decode("utf-8")
        m = re.search(r"X509v3 Subject Alternative Name:\s*(.*)", text)
        sans_list = []
        if m:
            sans_list = m.group(1).split(",")

        def _strip_prefix(s): return s.split(":")[1]  if  s.strip().startswith("DNS:")  else  s.strip()
        return map(_strip_prefix, sans_list)

    def get_must_staple(self):
        text = OpenSSL.crypto.dump_certificate(OpenSSL.crypto.FILETYPE_TEXT, self.cert).decode("utf-8")
        m = re.search(r"1.3.6.1.5.5.7.1.24:\s*\n\s*0....", text)
        return m

    @classmethod
    def validate_privkey(cls, privkey_path, passphrase=None):
        privkey_data = cls._load_binary_file(privkey_path)
        privkey = None
        if passphrase:
            privkey = OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM, privkey_data, passphrase)
        else:
            privkey = OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM, privkey_data)
        return privkey.check()

    def validate_cert_matches_priv_key(self, privkey_path):
        # Verifies that the private key and cert match.
        privkey_data = CertUtil._load_binary_file(privkey_path)
        privkey = OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM, privkey_data)
        context = OpenSSL.SSL.Context(OpenSSL.SSL.SSLv23_METHOD)
        context.use_privatekey(privkey)
        context.use_certificate(self.cert)
        context.check_privatekey()

    # --------- _utils_ ---------

    def _parse_tsp(self, tsp):
        # timestampss returned by PyOpenSSL are bytes
        # parse date and time part
        tsp_reformat = [tsp[0:4], b"-", tsp[4:6], b"-", tsp[6:8], b" ", tsp[8:10], b":", tsp[10:12], b":", tsp[12:14]]
        timestamp =  datetime.strptime(b"".join(tsp_reformat), '%Y-%m-%d %H:%M:%S')
        # adjust timezone
        tz_h, tz_m = 0, 0
        m = re.match(r"([+\-]\d{2})(\d{2})", b"".join([tsp[14:]]))
        if m:
            tz_h, tz_m = int(m.group(1)),  int(m.group(2))  if  tz_h > 0  else  -1 * int(m.group(2))
        return timestamp.replace(tzinfo = self.FixedOffset(60 * tz_h + tz_m))

    @classmethod
    def _load_binary_file(cls, path):
        with open(path, mode="rb")	 as file:
            return file.read()

    class FixedOffset(tzinfo):

        def __init__(self, offset):
            self.__offset = timedelta(minutes = offset)

        def utcoffset(self, dt):
            return self.__offset

        def tzname(self, dt):
            return None

        def dst(self, dt):
            return timedelta(0)
