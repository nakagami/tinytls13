##############################################################################
# Copyright (c) 2021 Hajime Nakagami<nakagami@gmail.com>
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright notice,
#  this list of conditions and the following disclaimer in the documentation
#  and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
##############################################################################
from tinytls13 import protocol
from tinytls13 import utils
from tinytls13 import x25519
from tinytls13 import hkdf
from tinytls13.tlscontext import TLSContext


class TLSSocket:
    def __init__(self, sock):
        self.sock = sock
        self.client_private = utils.urandom(32)
        self.client_public = x25519.base_point_mult(self.client_private)
        self.ctx = TLSContext(self.client_private)

    def client_hello(self):
        client_hello_message = protocol.client_hello_message(self.client_public)
        self.ctx.append_message(client_hello_message)
        self.sock.send(protocol.wrap_handshake(client_hello_message))

    def server_hello(self):
        head, message = protocol.read_content(self.sock)
        assert head[:3] == protocol.handshake + protocol.TLS12
        assert message[:1] == protocol.server_hello
        self.ctx.append_message(message)
        server_public = protocol.parse_server_hello(message)
        self.ctx.set_key_exchange(server_public)
        self.ctx.key_schedule_in_handshake()

    def server_handshake(self):
        finished = False
        while not finished:
            head, message = protocol.read_content(self.sock)
            if head[:1] == protocol.change_cipher_spec:
                # ignore change cipher spec
                continue
            # recieve application_data
            assert head[:3] == protocol.application_data + protocol.TLS12
            plaindata, content_type = self.ctx.server_traffic_crypto.decrypt_and_verify(message, head)
            while plaindata:
                _ = plaindata[:1]       # handshake type
                ln = utils.bytes_to_bint(plaindata[1:4])
                segment, plaindata = plaindata[:ln+4], plaindata[ln+4:]
                if segment[:1] == protocol.finished:
                    # recieve Finishied
                    verify_data = segment[4:]
                    assert len(verify_data) == 32
                    finished_key = hkdf.HKDF_expand_label(self.ctx.server_hs_traffic_secret, b'finished', b'', 32)
                    expected_verify_data = utils.hmac_sha256(
                        finished_key, hkdf.transcript_hash(self.ctx.get_messages())
                    )
                    assert verify_data == expected_verify_data
                    finished = True
                self.ctx.append_message(segment)

    def key_schedule(self):
        self.ctx.key_schedule_in_app_data()

    def send_finished(self):
        finished_key = hkdf.HKDF_expand_label(self.ctx.client_hs_traffic_secret, b'finished', b'', 32)
        verify_data = utils.hmac_sha256(finished_key, hkdf.transcript_hash(self.ctx.get_messages()))
        finished_content_type = protocol.finish_message(verify_data) + protocol.handshake
        message_pad = finished_content_type + utils.pad16(len(finished_content_type))
        tag_size = 16
        aad = protocol.application_data + protocol.TLS12 + utils.bint_to_bytes(len(message_pad) + tag_size, 2)
        encrypted = self.ctx.client_traffic_crypto.encrypt_and_tag(message_pad, aad)
        self.sock.send(protocol.wrap_encrypted(encrypted))

    def send_alert(self):
        message = b'\x02' + protocol.close_notify + protocol.alert
        message_pad = message + utils.pad16(len(message))
        tag_size = 16
        aad = protocol.application_data + protocol.TLS12 + utils.bint_to_bytes(len(message_pad) + tag_size, 2)
        encrypted = self.ctx.client_app_data_crypto.encrypt_and_tag(message_pad, aad)
        self.sock.send(protocol.wrap_encrypted(encrypted))

    def send(self, data):
        data_content_type = data + protocol.application_data
        message_pad = data_content_type + utils.pad16(len(data_content_type))
        tag_size = 16
        aad = protocol.application_data + protocol.TLS12 + utils.bint_to_bytes(len(message_pad) + tag_size, 2)
        encrypted = self.ctx.client_app_data_crypto.encrypt_and_tag(message_pad, aad)
        self.sock.send(protocol.wrap_encrypted(encrypted))

    def recv(self, ln):
        head, message = protocol.read_content(self.sock)
        plaindata, content_type = self.ctx.server_app_data_crypto.decrypt_and_verify(message, head)
        return plaindata

    def __enter__(self):
        return self

    def __exit__(self, exc, value, traceback):
        self.send_alert()
