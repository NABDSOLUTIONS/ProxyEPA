"""
Proxifier.
"""

# Basic imports
import socket
import datetime
import ssl
import logging
import base64
import os
import argparse
import json
import select
import ipaddress
import h11
import typing
import calendar
import struct
import signal
import sys
from textwrap import indent
from typing import Union, Tuple, List, Dict
from time import sleep
from binascii import a2b_hex

# Multiprocessing
from multiprocessing import Process

# Parsing requests
from io import BytesIO
from http.server import BaseHTTPRequestHandler
from http.client import HTTPResponse
from urllib.parse import urlparse

# Manage cryptography
import random
from OpenSSL import crypto
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from h11 import LocalProtocolError, RemoteProtocolError


#Manage Kerberos

from impacket.spnego import ASN1_AID, SPNEGO_NegTokenInit, SPNEGO_NegTokenResp, TypesMech
from impacket.krb5.asn1 import AP_REQ, Authenticator, TGS_REP, seq_set
from impacket.krb5.kerberosv5 import getKerberosTGT, getKerberosTGS
from impacket.krb5.types import Principal, KerberosTime, Ticket
from impacket.krb5.ccache import CCache
from impacket.krb5.kerberosv5 import KerberosError
from impacket.krb5 import constants
from impacket.ntlm import getNTLMSSPType1, getNTLMSSPType3
from impacket import ntlm
from pyasn1.codec.ber import encoder, decoder
from pyasn1.type.univ import noValue


RECV_SIZE = 1024
SOCKET_TIMEOUT = 2

# Logging defaults to INFO level
logging.basicConfig(level=logging.INFO)


class CertManager:
    """
    Should be in charge of creating certificates at runtime.
    """

    def __init__(self, cacert_path: str, cakey_path: str,
                 certsdir: str, cakey_passphrase: str = None):
        self.logger = logging.getLogger("Proxy.CertManager")

        self.certsdir = certsdir

        # Loading CA certificate and key
        with open(cacert_path, "rb") as cacert_file:
            self.cacert = crypto.load_certificate(crypto.FILETYPE_PEM, cacert_file.read())
        with open(cakey_path, "rb") as cakey_file:
            self.cakey = crypto.load_privatekey(
                crypto.FILETYPE_PEM, cakey_file.read(),
                passphrase=cakey_passphrase.encode() if cakey_passphrase is not None else None
            )

    def _get_cert_path(self, common_name):
        return os.path.join(self.certsdir, f"{common_name}.pem")

    def _get_key_path(self, common_name):
        return os.path.join(self.certsdir, f"{common_name}.key.pem")

    def _already_exists(self, common_name):
        return os.path.isfile(self._get_cert_path(common_name))\
            and os.path.isfile(self._get_key_path(common_name))

    def generate_ca(cert_path, key_path, key_pass=None):
        """."""
        key = crypto.PKey()
        key.generate_key(crypto.TYPE_RSA, 2048)

        cert = crypto.X509()
        cert.set_version(2)
        cert.set_serial_number(random.randint(50000000, 100000000))
        subject = cert.get_subject()
        subject.commonName = "WOLOLO"
        cert.set_issuer(subject)
        cert.set_pubkey(key)
        cert.gmtime_adj_notBefore(0)
        cert.gmtime_adj_notAfter(10*365*24*60*60)
        cert.add_extensions([crypto.X509Extension(b"basicConstraints", True, b"CA:TRUE")])

        # Signing
        cert.sign(key, 'sha256')
        with open(cert_path, "wb") as cert_file:
            cert_file.write(crypto.dump_certificate(crypto.FILETYPE_PEM, cert))
        with open(key_path, "wb") as key_file:
            key_file.write(crypto.dump_privatekey(crypto.FILETYPE_PEM, key, passphrase=key_pass))

        return cert_path, key_path

    def generate(self, common_name: str) -> tuple[str, str]:
        """Generates, if it does not already exists, a certificate signed by the CA
        provided during this object creation and returns the tuple (certificate,key)."""

        target_cert_path = self._get_cert_path(common_name)
        target_key_path = self._get_key_path(common_name)

        if self._already_exists(common_name):
            # No need to recreate it
            self.logger.debug("Certificate already exists, using existant")
            return target_cert_path, target_key_path

        # Generating certificate and private key
        self.logger.debug("Generating certificate for %s", common_name)
        key = crypto.PKey()
        key.generate_key(crypto.TYPE_RSA, 2048)

        cert = crypto.X509()
        cert.set_version(2)
        cert.set_serial_number(random.randint(50000000,100000000))
        subject = cert.get_subject()
        subject.commonName = common_name
        cert.set_issuer(self.cacert.get_subject())
        cert.set_pubkey(key)
        cert.gmtime_adj_notBefore(0)
        cert.gmtime_adj_notAfter(10*365*24*60*60)

        # Signing
        cert.sign(self.cakey, 'sha256')
        with open(target_cert_path, "wb") as tcert_file:
            tcert_file.write(crypto.dump_certificate(crypto.FILETYPE_PEM, cert))
        with open(target_key_path, "wb") as tkey_file:
            tkey_file.write(crypto.dump_privatekey(crypto.FILETYPE_PEM, key))

        return target_cert_path, target_key_path


def get_channel_bindings_from_cert(der_cert: bytes) -> bytes:
    """Returns the CBT associated with the certificate provided.
    See #FIXME article URL"""
    cert = x509.load_der_x509_certificate(der_cert, default_backend())

    hash_algorithm = cert.signature_hash_algorithm

    if hash_algorithm.name in ['md5', 'sha1']:
        digest = hashes.Hash(hashes.SHA256(), default_backend())
    else:
        digest = hashes.Hash(hash_algorithm, default_backend())

    digest.update(der_cert)
    der_cert_hash = digest.finalize()

    der_app_data = b"tls-server-end-point:" + der_cert_hash

    return der_app_data


class ConnectionHandler:
    """."""

    def __init__(self):
        self.sock = None
        self.ssock = None
        self.curr_sock = None
        self.logger = None
        self.conn: h11.Connection = None

    def stop(self):

        if self.conn.our_state is h11.SEND_RESPONSE:
            # We should be sending a response but there has been an error
            response = h11.Response(status_code=500, reason=b"Internal error", headers=[(b"Connection", b"close")])
            try:
                self._send([response, h11.EndOfMessage()])
            except BrokenPipeError:
                pass

        data = self.conn.send(h11.ConnectionClosed())
        if data is not None:
            try:
                self.curr_sock.sendall(data)
            except BrokenPipeError:
                pass

        try:
            self.curr_sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.curr_sock.close()

    def _send(self, events: List[Union[h11.Request, h11.Response, h11.InformationalResponse, h11.Data, h11.EndOfMessage, h11.ConnectionClosed]]):
        for event in events:
            self.logger.debug("Sending %s", pretty(event))
            data = self.conn.send(event)
            if data is not None:
                self.curr_sock.sendall(data)

    def _recv(self) -> List[Union[h11.Request, h11.Response, h11.Data, h11.EndOfMessage]]:
        """."""

        events = []
        while True:
            event = self.conn.next_event()
            assert type(event is not h11.PAUSED)

            if type(event) is h11.ConnectionClosed:
                self.logger.debug(f"Connection has closed, received {events}")
                return events

            elif type(event) is h11.NEED_DATA:
                # Need to make sure the client is not waiting for us before waiting for him
                if self.conn.they_are_waiting_for_100_continue:
                    self._send([h11.InformationalResponse(status_code=100, headers=[])])

                self.conn.receive_data(self.curr_sock.recv(RECV_SIZE))
                continue

            # An event ended, may be others
            events.append(event)

            if type(event) is h11.EndOfMessage:
                # Everything is fetched
                self.logger.debug("Received %s", pretty(events))
                return events


def pretty(
        events: Union[
            Union[
                h11.Request,
                h11.Response,
                h11.InformationalResponse,
                h11.Data,
                h11.EndOfMessage,
                h11.ConnectionClosed
            ],
            List[Union[
                h11.Request,
                h11.Response,
                h11.InformationalResponse,
                h11.Data,
                h11.EndOfMessage,
                h11.ConnectionClosed
            ]]
        ]
) -> str:
    if type(events) != list:
        events = [events]

    res = ""
    for event in events:
        if type(event) == h11.Request:
            res += f"\n\n{event.method.decode()} {event.target.decode()} HTTP/{event.http_version.decode()}"
            for header, val in event.headers:
                res += f"\n{header.decode()}: {val.decode()}"
            res += "\n"
        elif type(event) in [h11.InformationalResponse, h11.Response]:
            res += f"\n\nHTTP/{event.http_version.decode()} {event.status_code} {event.reason.decode()}"
            for header, val in event.headers:
                res += f"\n{header.decode()}: {val.decode()}"
            res += "\n"
        elif type(event) == h11.Data:
            res += f"DATA: \n\n{event.data.decode(errors='ignore')}\n"
        elif type(event) == h11.EndOfMessage:
            res += f"END with headers:"
            for header, val in event.headers:
                res += f"\n{header.decode(errors='ignore')}: {val.decode(errors='ignore')}"
        elif type(event) == h11.ConnectionClosed:
            res += "Connection closed"
        else:
            res += f"\n\n{str(res)}\n"
        return indent(res, "\t")

def to_text(obj, encoding='utf-8', errors='strict', nonstring='str'):
    if isinstance(obj, str):
        return obj
    elif isinstance(obj, bytes):
        return obj.decode(encoding, errors)

    if nonstring == 'str':
        try:
            obj = obj.__unicode__()
        except (AttributeError, UnicodeError):
            obj = _obj_str(obj, "")

        return to_text(obj, errors=errors, encoding=encoding)
    elif nonstring == 'passthru':
        return obj
    elif nonstring == 'empty':
        return ''
    else:
        raise ValueError("Invalid nonstring value '%s', expecting repr, passthru, or empty" % nonstring)


class ProxyToServerHelper(ConnectionHandler):
    """
    Connect to remote socket with TLS.
    """

    def __init__(self, use_tls: bool, hostname: str, port: int, use_kerberos):
        super().__init__()

        self.logger = logging.getLogger("Proxy.Proxy<->Server")

        self.conn = h11.Connection(our_role=h11.CLIENT)

        self.use_tls = use_tls  # Should the connection be on top of TLS

        self.ntlm_auth = {
            "negotiate": None,
            "challenge": None,
            "auth": None
        }
        self.use_kerberos = use_kerberos

        # Remote hostname & port
        self.hostname = hostname
        self.port = port

        self.logger.debug("Creating new ProxyToServerHelper(%s, %s)", hostname, port)

        self.logger.debug("Creating socket & connecting")
        self.sock = socket.create_connection((self.hostname, self.port), timeout=5)

        self.curr_sock = self.sock
        # PROTOCOL_TLS_CLIENT requires valid cert chain and hostname
        if self.use_tls:
            self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            # Do not verify remote server certificate, may change in the future
            self.context.check_hostname = False
            self.context.verify_mode = ssl.CERT_NONE
            # Wrap in TLS
            self.ssock = self.context.wrap_socket(self.sock, server_hostname=hostname)
            self.curr_sock = self.ssock

    def _dump_server_cert(self) -> bytes:
        """Returns the certificate of the remote server as DER format (as bytes)."""
        if self.use_tls:
            return self.ssock.getpeercert(binary_form=True)
        raise RuntimeError("Tried to dump remote server certificate in a non-TLS session")

    def get_channel_bindings(self):
        try:
            # Getting remote certificate
            srv_cert = self._dump_server_cert()
            # Generating channel binding stuff
            cbt = get_channel_bindings_from_cert(srv_cert)
            self.logger.debug("Channel binding token %s.", cbt)
        except RuntimeError:
            # Could not generate channel binding token -> leaving it empty
            return None

        token_len = len(cbt)
        writer = b"\x00"*16
        writer += struct.pack('I', token_len)
        digest = hashes.Hash(hashes.MD5(), default_backend())
        digest.update(writer)
        digest.update(cbt)
        return digest.finalize()

    def kerberos_auth(self, domain, username, password, kdc_host, useCache=True):

        TGS = None
        if useCache is True :
            try:
                ccache = CCache.loadFile(os.getenv('KRB5CCNAME'))
            except:
                self.logger.warning("No cache present, using provided credential")
                user = Principal(username, type=constants.PrincipalNameType.NT_PRINCIPAL.value)
                tgt, cipher, oldSessionKey, sessionKey = getKerberosTGT(user, password, domain, "", "", "", kdc_host)
                spn = self.hostname.split("." + domain)[0]
 
            else:
                self.logger.warning("Using Kerberos Cache")
                # retrieve domain information from CCache file if needed
                domain = ccache.principal.realm['data'].decode('utf-8')
                self.logger.debug('Domain retrieved from CCache: %s' % domain)

                spn = self.hostname.replace("." + domain, "")
                principal = 'http/%s@%s' % (spn.upper(),domain.upper())
                creds = ccache.getCredential(principal)
                if creds is None:
                    principal = 'krbtgt/%s@%s' % (domain.upper(),domain.upper())
                    creds =  ccache.getCredential(principal)
                    if creds is not None:
                        TGT = creds.toTGT()
                        tgt = TGT['KDC_REP']
                        cipher = TGT['cipher']
                        sessionKey = TGT['sessionKey']
                        self.logger.warning('Using TGT from cache')
                    else:
                        self.logger.debug("No valid credentials found in cache. ")
                        pass
                else:
                    TGS = creds.toTGS()
                    self.logger.debug('Using TGS from cache')


                # retrieve user information from CCache file if needed
                if creds is not None:
                    user = creds['client'].prettyPrint().split(b'@')[0].decode('utf-8')
                    self.logger.debug('Username retrieved from CCache: %s' % user)
                    user = Principal(user, type=constants.PrincipalNameType.NT_PRINCIPAL.value)
                elif len(ccache.principal.components) > 0:
                    user = ccache.principal.components[0]['data'].decode('utf-8')
                    user = Principal(user, type=constants.PrincipalNameType.NT_PRINCIPAL.value)
                    self.logger.debug('Username retrieved from CCache: %s' % user)


        
        
        if TGS is None :
           serverName = Principal('HTTP/' + spn, type=constants.PrincipalNameType.NT_SRV_INST.value)
           tgs, cipher, oldSessionKey, sessionKey = getKerberosTGS(serverName, domain, kdc_host, tgt, cipher, sessionKey)
        else:
           tgs = TGS['KDC_REP']
           sessionKey = TGS['sessionKey']
           cipher = TGS['cipher']



        blob = SPNEGO_NegTokenInit()
        blob['MechTypes'] = [TypesMech['MS KRB5 - Microsoft Kerberos 5'], TypesMech['KRB5 - Kerberos 5'], TypesMech['NEGOEX - SPNEGO Extended Negotiation Security Mechanism'], TypesMech['NTLMSSP - Microsoft NTLM Security Support Provider']]


        tgs = decoder.decode(tgs, asn1Spec=TGS_REP())[0]
        ticket = Ticket()
        ticket.from_asn1(tgs['ticket'])

        apReq = AP_REQ()
        apReq['pvno'] = 5
        apReq['msg-type'] = int(constants.ApplicationTagNumbers.AP_REQ.value)

        opts = [2]
        apReq['ap-options'] = constants.encodeFlags(opts)
        seq_set(apReq, 'ticket', ticket.to_asn1)

        authenticator = Authenticator()
        authenticator['authenticator-vno'] = 5
        authenticator['crealm'] = domain
        seq_set(authenticator, 'cname', user.components_to_asn1)
        now = datetime.datetime.utcnow()

        authenticator['cusec'] = now.microsecond
        authenticator['ctime'] = KerberosTime.to_asn1(now)
        encodedAuthenticator = encoder.encode(authenticator)
        encryptedEncodedAuthenticator = cipher.encrypt(sessionKey, 11, encodedAuthenticator, None)

        apReq['authenticator'] = noValue
        apReq['authenticator']['etype'] = cipher.enctype
        apReq['authenticator']['cipher'] = encryptedEncodedAuthenticator

        blob['MechToken'] = encoder.encode(apReq)
        return base64.b64encode(blob.getData())

    def gen_ntlm_negotiate(self) -> bytes:
        """Returns a NEGOTIATE_MESSAGE base64 encoded message."""

        negotiate = getNTLMSSPType1(workstation="", domain="", signingRequired=True)
        #negotiate["flags"] = ntlm.NTLMSSP_NEGOTIATE_EXTENDED_SESSIONSECURITY | ntlm.NTLMSSP_NEGOTIATE_ALWAYS_SIGN | ntlm.NTLMSSP_NEGOTIATE_NTLM | ntlm.NTLM_NEGOTIATE_OEM | ntlm.NTLMSSP_REQUEST_TARGET | ntlm.NTLMSSP_NEGOTIATE_UNICODE

        # Saving this message
        self.ntlm_auth["negotiate"] = negotiate

        return base64.b64encode(negotiate.getData())

    def gen_ntlm_auth(self, creds, lmhash, nthash) -> bytes:
        """Returns an AUTHENTICATE_MESSAGE from the previous messages."""

        def myComputeResponseNTLMv2(flags, serverChallenge, clientChallenge, serverName, domain, user, password, lmhash='', nthash='', use_ntlmv2=ntlm.USE_NTLMv2):
            """Rewriten method to override the default Impacket one as it does
            not allow to specify the correct target name (always "cifs/..."),
            nor permit to add the channel bindings value.
            Has to be defined dynamically to include the current value of
            channel bindings."""
        
            responseServerVersion = b'\x01'
            hiResponseServerVersion = b'\x01'
            responseKeyNT = ntlm.NTOWFv2(user, password, domain, nthash)
        
            av_pairs = ntlm.AV_PAIRS(serverName)
            av_pairs[ntlm.NTLMSSP_AV_TARGET_NAME] = 'http/'.encode('utf-16le') + av_pairs[ntlm.NTLMSSP_AV_HOSTNAME][1]
            channel_bindings = self.get_channel_bindings()
            if channel_bindings is not None:
                av_pairs[ntlm.NTLMSSP_AV_CHANNEL_BINDINGS] = channel_bindings
            if av_pairs[ntlm.NTLMSSP_AV_TIME] is not None:
               aTime = av_pairs[ntlm.NTLMSSP_AV_TIME][1]
            else:
               aTime = struct.pack('<q', (116444736000000000 + calendar.timegm(time.gmtime()) * 10000000) )
               av_pairs[ntlm.NTLMSSP_AV_TIME] = aTime
            serverName = av_pairs.getData()
        
            temp = responseServerVersion + hiResponseServerVersion + b'\x00' * 6 + aTime + clientChallenge + b'\x00' * 4 + \
                   serverName + b'\x00' * 4
        
            ntProofStr = ntlm.hmac_md5(responseKeyNT, serverChallenge + temp)
        
            ntChallengeResponse = ntProofStr + temp
            lmChallengeResponse = ntlm.hmac_md5(responseKeyNT, serverChallenge + clientChallenge) + clientChallenge
            sessionBaseKey = ntlm.hmac_md5(responseKeyNT, ntProofStr)
        
            return ntChallengeResponse, lmChallengeResponse, sessionBaseKey

        # Overriding the impacket default method
        ntlm.computeResponseNTLMv2 = myComputeResponseNTLMv2
        auth, exported_session_key = getNTLMSSPType3(self.ntlm_auth["negotiate"], self.ntlm_auth["challenge"], creds["username"], creds["password"] , creds["domain"], lmhash, nthash)
        return base64.b64encode(auth.getData())

    def check_auth(self, http_response: List[Union[h11.Response, h11.Data]], creds: Dict[str, str], lmhash, nthash) -> Union[None, bytes]:
        """Given an HTTP response, will generate a corresponding NTLM authentication HTTP
        header value or will return None. May raise RuntimeException in case there is a problem.
        """

        # Parsing HTTP headers to retrive WWW-Authenticate headers
        headers = http_response[0].headers
        www_authenticate = []  # List of WWW-Authenticate headers' values
        for header in headers:
            if header[0] == b"www-authenticate":
                www_authenticate.append(header[1])
        if len(www_authenticate) == 0:
            # No WWW-Authenticate header, there is nothing to do
            return None

        if not any(val.startswith(b"NTLM") or val.startswith(b"Negotiate") for val in www_authenticate):
            # Not NTLM nor kerberos authentication
            return None

        use_ntlm = False
        use_kerberos = False
        token = None
        for header_val in www_authenticate:
            if header_val.startswith(b"NTLM") and not self.use_kerberos:
                use_ntlm = True
                prepend = b"NTLM "
                token = header_val[5:]
                break  # This is the default for NTLM
            elif header_val.startswith(b"Negotiate") and not self.use_kerberos:
                use_ntlm = True
                prepend = b"Negotiate "
                token = header_val[10:]
            elif header_val.startswith(b"Negotiate"):
                use_kerberos = True
                prepend = b"Negotiate "
                token = header_val[10:]
                break  # This is the default for Kerberos

        if use_ntlm:
            if len(token) == 0:
                # First 401 from server
                # Sending NTLM_NEGOTIATE
                try:
                    new_token = self.gen_ntlm_negotiate()
                    return prepend + new_token
                except TypeError:
                    # This is the second time we received a NTLM NEGOTIATE message
                    # meaning that the authentication failed. We just forward the response
                    # to the client
                    self.logger.warning("Authentication failed.")
                    raise RuntimeError("Authentication failed.")
            elif token.startswith(b"YIG"):
                # This is a Negotiate:Kerberos response
                self.logger.warning("Remote server only accepts Kerberos authentication, consider using -k option.")
                raise RuntimeError("Authentication failed.")
            else:
                # Got CHALLENGE message from server
                # Sending NTLM_AUTH
                self.logger.debug("Got NTLM CHALLENGE from %s", prepend.decode().strip())
                self.ntlm_auth["challenge"] = base64.b64decode(token)
                new_token = self.gen_ntlm_auth(creds,lmhash,nthash)
                return prepend + new_token
        elif use_kerberos:
            if token.startswith(b"oYGh"):
                # Response to final kerberos message, nothing to do
                #TODO: analyse the response to check for errors
                return None

            # Simply include the kerberos AP_REQ
            return prepend + self.kerberos_auth(creds["domain"], creds["username"], creds["password"], creds["kdc_host"])

    def send(self, events: List[Union[h11.Response, h11.Data, h11.EndOfMessage]]) -> None:
        """Sending request to remote server."""

        self.logger.debug("Our state: %s; their state: %s", self.conn.our_state, self.conn.their_state)
        assert self.conn.our_state is h11.IDLE and self.conn.their_state is h11.IDLE

        if not self.use_tls:
            # Modifying the request from "GET http://google.fr/test HTTP/1.1" to "GET /test HTTP/1.1"
            self.logger.debug("Modifying %s", pretty(events[0]))
            path = urlparse(events[0].target).path
            events[0].target = path

        self._send(events)

    def recv(self) -> List[Union[h11.Response, h11.Data, h11.EndOfMessage]]:
        """Receiving server response."""

        self.logger.debug("Our state: %s; their state: %s", self.conn.our_state, self.conn.their_state)
        assert self.conn.our_state in [h11.DONE, h11.MUST_CLOSE, h11.CLOSED] and self.conn.their_state is h11.SEND_RESPONSE

        try:
            received = self._recv()
        except ssl.SSLError:
            self.logger.error(e, exc_info=self.logger.getEffectiveLevel() == logging.DEBUG)
            self.stop()

        if self.conn.our_state is h11.MUST_CLOSE or self.conn.their_state is h11.MUST_CLOSE:
            self.stop()

        return received


class ClientToProxyHelper(ConnectionHandler):
    """
    Server that hijacks the client to legitimate server connection.
    """

    def __init__(self, sock, cert_manager):
        super().__init__()

        self.logger = logging.getLogger("Proxy.Client<->ProxyHelper")

        self.logger.debug("Creating new ClientToProxyHelper")

        self.conn = h11.Connection(our_role=h11.SERVER)

        self.cert_manager = cert_manager

        self.sock = sock
        self.curr_sock = self.sock


        self.use_tls = False
        self.remote_host = None
        self.remote_port = None
        self.context = None

    def _setup_context(self, _: ssl.SSLSocket, server_name: str, context: ssl.SSLContext):
        """Will be call when the client has sent the client hello handshake message.
        In that case, we are able to check the SNI and update the context.
        """
        if server_name is None:
            self.logger.warning("No SNI provided, still cannot determine server name.")
            raise RuntimeError("Cannot determine server name.")

        self.remote_host = server_name

        # We can now generate the correct certificate
        cert_path, key_path = self.cert_manager.generate(self.remote_host)

        context = ssl.SSLContext(ssl.PROTOCOL_TLS)
        context.load_cert_chain(
            certfile=cert_path,
            keyfile=key_path,
            password=None
        )

    def send(self, events: List[Union[h11.Response, h11.Data, h11.EndOfMessage]]):
        """Send response to client."""

        self.logger.debug("Our state: %s; their state: %s", self.conn.our_state, self.conn.their_state)
        assert self.conn.our_state is h11.SEND_RESPONSE and self.conn.their_state in [h11.DONE, h11.MIGHT_SWITCH_PROTOCOL, h11.MUST_CLOSE]

        self._send(events)

        if self.conn.our_state is h11.MUST_CLOSE or self.conn.their_state is h11.MUST_CLOSE:
            self.stop()

    def recv(self) -> List[Union[h11.Request, h11.Data, h11.EndOfMessage]]:
        """Receiving server response."""

        self.logger.debug("Our state: %s; their state: %s", self.conn.our_state, self.conn.their_state)
        assert self.conn.our_state is h11.IDLE and self.conn.their_state is h11.IDLE

        return self._recv()

    def wrap(self) -> None:
        """Wrap current socket in TLS. Raises an error if already done."""

        if self.ssock is not None:
            raise RuntimeError("Socket already wrapped.")

        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS)

        if self.remote_host is None:
            self.context.sni_callback(self._setup_context)
        else:
            # Determining custom certificates as we already know the server name
            cert_path, key_path = self.cert_manager.generate(self.remote_host)

            self.context.load_cert_chain(
                certfile=cert_path,
                keyfile=key_path,
                password=None
            )

        self.logger.debug("Wrapping initial client to proxy socket")

        self.ssock = self.context.wrap_socket(sock=self.sock, server_side=True)

        self.curr_sock = self.ssock
        self.logger.debug("Wrapped")

    def prepare(self, request_list: List[Union[h11.Request, h11.Data]]) -> Tuple[str, int]:

        request = request_list[0]
        target_parsed = urlparse(request.target)
        if target_parsed.netloc == b"":
            # Try prepending //
            target_parsed = urlparse(b"//" + request.target)

        netloc = target_parsed.netloc.decode().split(":")
        host = netloc[0]

        if request.method == b"CONNECT":
            # This is a CONNECT request, will probably be a TLS session
            # Responding OK to client
            self.use_tls = True

            # Hostname
            try:
                ipaddress.ip_address(host)
            except ValueError:
                # This is not an IP address
                # We can keep the provided value
                self.remote_host = host
            else:
                # This is an IP address, we need to find the hostname associated
                # 2 possibilities: from the SNI in the TLS session ; from the Host header
                try:
                    self.remote_host = request.headers["Host"].decode()
                except ValueError:
                    # No header host, we will need to rely on the SNI
                    pass

            # Port
            try:
                self.remote_port = int(netloc[1])
            except ValueError:
                # No port provided
                if target_parsed.scheme.lower() == "http":
                    self.remote_port = 80
                else:
                    # We default to port 443
                    self.remote_port = 443

            # Response in plaintext HTTP
            connect_response = h11.Response(status_code=200, headers=[])
            self.send([connect_response])  # No EndOfMessage here as h11 thinks its job is done here, protocol is
            # switched to something else (because we answered 200 to a CONNECT request). Therefore we need to create a
            # new conn to follow the inner HTTP exchange
            self.conn = h11.Connection(our_role=h11.SERVER)

            # Wrapping in TLS session (MitM), hopping that the client is looking
            # for a TLS session
            self.wrap()

        else:
            # This is not HTTPS, probably straight HTTP
            self.use_tls = False
            self.remote_host = netloc[0]
            if len(netloc) == 1:
                self.remote_port = 80
            else:
                self.remote_port = int(netloc[1])

        return self.remote_host, self.remote_port


class Proxy:
    """
    Listens on a local port for SSL connections.
    """

    def __init__(self, listen_address: str, listen_port: int, cert_manager: CertManager, dcip: str="", lmhash: str="", nthash: str="", use_kerberos: bool = False, is_multiprocess: bool = False, creds: dict = None):
        self.logger = logging.getLogger("Proxy")

        signal.signal(signal.SIGINT, self.interrupted)

        self.prx_sock = None  # Proxy socket that waits for client connections

        self.listen_address = listen_address
        self.listen_port = listen_port

        self.cert_manager = cert_manager  # Will handle the creation of all certificates

        self.is_multiprocess = is_multiprocess  # Will the program run in multiple processes

        self.creds = creds  # List of credentials
        self.lmhash = '' 
        self.nthash = ''
        
        if lmhash != '' or nthash != '':
            if len(lmhash) % 2:     lmhash = '0%s' % lmhash
            if len(nthash) % 2:     nthash = '0%s' % nthash
            try: # just in case they were converted already
                self.lmhash = a2b_hex(lmhash)
                self.nthash = a2b_hex(nthash)
            except:
                pass
        self.use_kerberos = use_kerberos # Will perform kerberos authentication if available
        self.kdc_host = dcip # ip address of the KDC for kerberos authentication

    def __enter__(self):
        self.logger.debug("Entered proxy, creating sockets.")
        self.prx_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
        self.prx_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.logger.debug("Proxy socket created.")
        return self

    def __exit__(self, *_):
        self.logger.info("Exiting...")
        self.prx_sock.close()

    def _remove_proxy_headers(self, req: h11.Request) -> h11.Request:
        """Removes every Proxy-* headers. Will also force HTTP version to 1.1."""
        initial_headers = req.headers.raw_items()
        headers = []
        for header in initial_headers:
            if header[0].decode().lower().startswith("proxy-"):
                continue
            headers.append(header)
        return h11.Request(method=req.method, target=req.target, headers=headers, http_version=b"1.1")

    def _add_header(self, req: h11.Request, newh: List[Tuple[bytes, bytes]]) -> h11.Request:
        """Returns a h11.Request with the new header included. Will also force HTTP version to 1.1."""
        return h11.Request(
            method=req.method,
            target=req.target,
            headers=req.headers.raw_items() + newh,  # List concat
            http_version=b"1.1"
        )

    def _force_http_version(self, resp: h11.Response) -> h11.Response:
        return h11.Response(
            headers=resp.headers.raw_items(),
            status_code=resp.status_code,
            http_version=b"1.1",
            reason=resp.reason
        )

    def interrupted(self, signal, frame):
        self.logger.info("Stopping proxy")
        sys.exit(0)

    def run(self) -> None:
        """Starts the proxy."""

        # Binding socket
        self.logger.debug("Binding proxy socket.")
        self.prx_sock.bind((self.listen_address, self.listen_port))
        self.prx_sock.listen(20)  # Queue of 20 unaccepted connections before refusing connections
        self.logger.info("Proxy socket bound, listening on %s:%s.", self.listen_address, self.listen_port)

        while True:
            clt2prx_con, _ = self.prx_sock.accept()
            self.logger.info("Got connection from %s:%s.", *clt2prx_con.getpeername())
            if self.is_multiprocess:
                self.logger.debug("Creating new process.")
                p = Process(target=self.handle_connection, args=(clt2prx_con,))
                p.start()
            else:
                try:
                    self.handle_connection(clt2prx_con)
                except Exception as e:
                    self.logger.error(e, exc_info=self.logger.getEffectiveLevel() == logging.DEBUG)
                    return

    def _get_creds(self, srv_host):
        _ = "_"
        if srv_host in self.creds:
            self.logger.debug("Got credentials for %s.", srv_host)
            _ = srv_host
        username = self.creds[_]["username"]
        password = self.creds[_]["password"]
        self.logger.debug("Using user %s with password %s for %s.", username, password, srv_host)
        return username, password
    
    def split_username(self, username: typing.Optional[str]) -> typing.Tuple[typing.Optional[str], typing.Optional[str]]:
        if username is None:
            return None, None

        domain: typing.Optional[str]
        if '\\' in username:
            domain, username = username.split('\\', 1)
        else:
            domain = None

        return to_text(domain, nonstring='passthru'), to_text(username, nonstring='passthru')

    def handle_connection(self, clt2prx_con: socket.socket) -> None:
        """Handle a client connection to the proxy."""
        self.logger.debug("Handling connection.")
        clt2prx_hdler = ClientToProxyHelper(clt2prx_con, self.cert_manager)
        try:
            # Receiving initial client request (may be CONNECT request)
            clt_events = clt2prx_hdler.recv()
        except (LocalProtocolError, RemoteProtocolError):
            self.logger.warning("Wrong initial request received from client")
            return

        # Establish TLS if needed (between client and proxy)
        try:
            srv_host, srv_port = clt2prx_hdler.prepare(clt_events)
        except ssl.SSLError as e:
            self.logger.error("SSL Error: %s", e, exc_info=self.logger.getEffectiveLevel() == logging.DEBUG)
            clt2prx_hdler.stop()
            return
        except Exception as e:
            self.logger.error("Unknown error: %s", e, exc_info=self.logger.getEffectiveLevel() == logging.DEBUG)
            clt2prx_hdler.stop()
            return

        try:
            # Create handler to manage connection between proxy and server
            prx2srv_hdler = ProxyToServerHelper(clt2prx_hdler.use_tls, srv_host, srv_port, self.use_kerberos)
        except (TimeoutError, socket.timeout):
            self.logger.warning("Request to %s:%s timed out.", srv_host, srv_port)
            clt2prx_hdler.stop()
            return
        except ConnectionRefusedError:
            self.logger.warning("Request could not be performed to %s:%s: connection refused.",
                                srv_host, srv_port)
            clt2prx_hdler.stop()
            return
        except OSError as e:
            self.logger.error("Error while connecting to remote server: %s", e, exc_info=self.logger.getEffectiveLevel() == logging.DEBUG)
            clt2prx_hdler.stop()
            return
        except ssl.SSLError as e:
            self.logger.error("SSL error while connecting to remote server: %s", e, exc_info=self.logger.getEffectiveLevel() == logging.DEBUG)
            clt2prx_hdler.stop()
            return
        except Exception as e:
            self.logger.error("Unknown error: %s", e, exc_info=self.logger.getEffectiveLevel() == logging.DEBUG)
            clt2prx_hdler.stop()
            return

        if clt2prx_hdler.use_tls:
            # In case the client uses TLS, the first request is a CONNECT request,
            # we need to receive another one to get the "real" request
            self.logger.debug("Client is using TLS, getting a second request (after a CONNECT request)")
            try:
                clt_events = clt2prx_hdler.recv()
            except (LocalProtocolError, RemoteProtocolError):
                self.logger.warning(
                    "The client did not send a correct second request after the initial CONNECT request."
                )
                return

        # Current status of NTLM authentication for this proxy2server connection (may not be used)
        username, password = self._get_creds(srv_host)
        domain, username = self.split_username(username)

        # Prepare credential object
        cur_creds = {
            "username": username,
            "password": password,
            "domain": domain,
            "kdc_host": self.kdc_host
        }

        last_loop = False
        while True:
            # Forwarding traffic

            if len(clt_events) == 0:
                # No request received from client/connection was closed
                break

            # Removing proxy headers
            clt_events[0] = self._remove_proxy_headers(clt_events[0])

            init_req = clt_events[0]
            while True:
                # Perform authentication
                prx2srv_hdler.send(clt_events)
                try:
                    srv_resp = prx2srv_hdler.recv()
                except (LocalProtocolError, RemoteProtocolError, socket.timeout):
                    self.logger.warning("Server did not respond correctly to proxy request.")
                    return

                if prx2srv_hdler.conn.our_state is h11.CLOSED or prx2srv_hdler.conn.their_state is h11.CLOSED:
                    # No use to perform authentication (TCP connection is closed), just forward response
                    prx2srv_hdler.stop()
                    last_loop = True
                    break

                # Check if there is an Authentication header, and generates the corresponding header if so
                try:
                    authorization_header = prx2srv_hdler.check_auth(srv_resp, cur_creds, self.lmhash, self.nthash)
                except RuntimeError:
                    self.logger.warning("Error while performing authentication, stopping.")
                    prx2srv_hdler.stop()
                    last_loop = True
                    break
                if authorization_header is None:
                    # No authentication or the authentication is complete
                    try:
                        prx2srv_hdler.conn.start_next_cycle()
                    except LocalProtocolError:
                        prx2srv_hdler.stop()
                        last_loop = True
                    break

                # Adding new authorization header to the request
                clt_events[0] = self._add_header(init_req, [(b"Authorization", authorization_header)])
                prx2srv_hdler.conn.start_next_cycle()

            # Need to patch the HTTP version returned by the server, otherwise h11 won't let us forward it to the client
            # if it is not 1.1. Hopefully it will be compatible...
            srv_resp[0] = self._force_http_version(srv_resp[0])
            # Sending the server response back to the client
            clt2prx_hdler.send(srv_resp)
            if clt2prx_hdler.conn.our_state is h11.CLOSED or clt2prx_hdler.conn.their_state is h11.CLOSED:
                last_loop = True

            if last_loop:
                # The server/client connection was closed
                break

            # Receiving next client request
            try:
                clt2prx_hdler.conn.start_next_cycle()
                clt_events = clt2prx_hdler.recv()
            except (LocalProtocolError, RemoteProtocolError):
                # Client closed connection
                self.logger.info("Client did not send correct HTTP request.")
                break

        clt2prx_hdler.stop()
        prx2srv_hdler.stop()


def main():
    """
    Entry point.
    """

    # Parsing command line arguments
    parser = argparse.ArgumentParser(description="Simple HTTP proxy that support NTLM EPA.")

    # Proxy listening options
    parser.add_argument("--listen-address", "-l", default="127.0.0.1",
                        help="Address the proxy will be listening on, defaults to 127.0.0.1.")
    parser.add_argument("--listen-port", "-p", default=3128, type=int,
                        help="Port the proxy will be listening on, defaults to 3128.")

    # CA options
    parser.add_argument("--cacert", default="./cacert.pem",
                        help="Filepath to the CA certificate, defaults to ./cacert.pem.\
                        Will be created if it does not exists.")
    parser.add_argument("--cakey", default="./cakey.pem",
                        help="Filepath to the CA private key, defaults to ./cakey.pem.\
                        Will be created if it does not exists.")
    parser.add_argument("--cakey-pass", default=None,
                        help="CA private key passphrase.")

    # Generated certificates options
    parser.add_argument("--certsdir", default="/tmp/Prox-Ez",
                        help="Path to the directory the generated certificates will be stored in,\
                        defaults to /tmp/Prox-Ez. Will be created if it does not exists.")

    # Multiprocessing
    parser.add_argument("--singleprocess", "-sp", action="store_true",
                        help="Do you want to be slowwwww ?! Actually useful during debug.")

    # Debug
    parser.add_argument("--debug", "-d", action="store_true",
                        help="Increase debug output.")

    # Credentials options
    parser.add_argument("--creds",
                        help="""Path to the credentials file, for instance:
{
    "my.hostname.com": {
        "username": "domain\\user",
        "password": "password"
    }, "my.second.hostname.com": {
        "username": "domain1\\user1",
        "password": "password1"
    }
}
""")
    parser.add_argument("--default_username", "-du", default="user",
                        help="Default username to use. In the form domain\\\\user.")
    parser.add_argument("--default_password", "-dp", default="password",
                        help="Default password to use.")
    parser.add_argument("--hashes", help="could be used instead of default_password. format: lmhash:nthash")

    # Kerberos authentication options
    parser.add_argument("--kerberos", "-k", action="store_true", help="Enable kerberos authentication instead of NTLM")
    parser.add_argument('--dcip', action='store',  help="IP Address of the domain controller (only for kerberos)")


    args = parser.parse_args()

    if args.debug:
        logging.getLogger("Proxy").setLevel(logging.DEBUG)

    # Handling parameters
    if not os.path.isdir(args.certsdir):
        os.mkdir(args.certsdir)
    if not (os.path.isfile(args.cacert) and os.path.isfile(args.cakey)):
        CertManager.generate_ca(args.cacert, args.cakey)

    # Certificate manager
    cert_manager = CertManager(args.cacert, args.cakey, args.certsdir, args.cakey_pass)

    # Loading credentials from file if provided
    credentials = {}
    if args.creds is not None:
        with open(args.creds, "r") as creds_file:
            credentials = json.load(creds_file)
    # Default credentials (arbitrarily associated with hostname "_")
    credentials.update({
        "_": {
            "username": args.default_username,
            "password": args.default_password
        }
    })
    lmhash = ""
    nthash = ""
    if args.hashes is not None:
        lmhash, nthash = args.hashes.split(':')

    with Proxy(args.listen_address, args.listen_port, cert_manager, args.dcip, lmhash, nthash, use_kerberos=args.kerberos, is_multiprocess=not args.singleprocess, creds=credentials) as proxy:
        proxy.run()


if __name__ == "__main__":
    main()
