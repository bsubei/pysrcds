# Copyright (C) 2013 Peter Rowlands
"""Source server RCON communications module"""

import itertools
import re
import struct
import socket


# Packet types
SERVERDATA_AUTH = 3
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_EXECCOMMAND = 2
SQUAD_CHAT_STREAM = 1
SERVERDATA_RESPONSE_VALUE = 0
# NOTE(bsubei): I completely invented this type just to internally signal end of multipacket response.
END_OF_MULTIPACKET = 77

# Special response by the Squad server to indicate the end of a multipacket response.
SPECIAL_MULTIPACKET_RESPONSE = b'\x00\x01\x00\x00\x00\x00\x00'


class RconPacket(object):
    """RCON packet"""

    def __init__(self, pkt_id=0, pkt_type=-1, body=''):
        self.pkt_id = pkt_id
        self.pkt_type = pkt_type
        self.body = body

    def __str__(self):
        """Return the body string."""
        return self.body

    def size(self):
        """Return the pkt_size field for this packet."""
        return len(self.body) + 10

    def pack(self):
        """Return the packed version of the packet."""
        return struct.pack('<3i{0}s'.format(len(self.body) + 2),
                           self.size(), self.pkt_id, self.pkt_type,
                           bytearray(self.body, 'utf-8'))


class RconConnection(object):
    """RCON client to server connection"""

    def __init__(self, server, port=27015, password='', single_packet_mode=False):
        """Construct an RconConnection.

        Parameters:
            server (str) server hostname or IP address
            port (int) server port number
            password (str) server RCON password
            single_packet_mode (bool) set to True for servers which do not hand 0-length SERVERDATA_RESPONSE_VALUE
                requests (i.e. Factorio).

        """
        self.server = server
        self.port = port
        self.single_packet_mode = single_packet_mode
        self._sock = socket.create_connection((server, port))
        self.pkt_id = itertools.count(1)
        self.chat_messages = []
        self._authenticate(password)

    def _authenticate(self, password):
        """Authenticate with the server using the given password."""
        auth_pkt = RconPacket(next(self.pkt_id), SERVERDATA_AUTH, password)
        self._send_pkt(auth_pkt)
        # The server should respond with a SERVERDATA_RESPONSE_VALUE followed by SERVERDATA_AUTH_RESPONSE.
        # Note that some server types omit the initial SERVERDATA_RESPONSE_VALUE packet.
        auth_resp = self.read_response(auth_pkt)
        if auth_resp.pkt_type == SERVERDATA_RESPONSE_VALUE:
            auth_resp = self.read_response()
        if auth_resp.pkt_type != SERVERDATA_AUTH_RESPONSE:
            raise RconError('Received invalid auth response packet')
        if auth_resp.pkt_id == -1:
            raise RconAuthError('Bad password')

    def exec_command(self, command):
        """Execute the given RCON command.

        Parameters:
            command (str) the RCON command string (ex. "status")

        Returns the response body
        """
        cmd_pkt = RconPacket(next(self.pkt_id), SERVERDATA_EXECCOMMAND,
                             command)
        self._send_pkt(cmd_pkt)
        resp = self.read_response(cmd_pkt, multi=True)
        return resp.body

    def _send_pkt(self, pkt):
        """Send one RCON packet over the connection.

            Raises:
                RconSizeError if the size of the specified packet is > 4096 bytes
                RconError if the received packet header is malformed
        """
        if pkt.size() > 4096:
            raise RconSizeError('pkt_size > 4096 bytes')
        data = pkt.pack()
        self._sock.sendall(data)

    def _recv_pkt(self):
        """Read one RCON packet"""
        # The header is made of three little-endian integers (8 bytes each).
        HEADER_SIZE = struct.calcsize('<3i')
        while True:
            # Skip empty packets and try again.
            header = self._sock.recv(HEADER_SIZE)
            if len(header) != 0:
                break

        # We got a weird packet here! If it's the special multipacket message, there is nothing left to read for this
        # packet. Otherwise, it's a malformed packet header.
        if header == SPECIAL_MULTIPACKET_RESPONSE:
            return RconPacket(-1, END_OF_MULTIPACKET, '')
        if len(header) != HEADER_SIZE:
            raise RconError('Received malformed packet header!')

        (pkt_size, pkt_id, pkt_type) = struct.unpack('<3i', header)
        body = self._sock.recv(pkt_size - 8)
        return RconPacket(pkt_id, pkt_type, body)

    def read_response(self, request=None, multi=False):
        """Return the next response packet.

        Parameters:
            request (RconPacket) if request is provided, read_response() will check that the response ID matches the
                specified request ID
            multi (bool) set to True if read_response() should check for a multi packet response. If the current
                RconConnection has single_packet_mode enabled, this parameter is ignored.

        Raises:
            RconError if an error occurred while receiving the server response
        """
        if request and not isinstance(request, RconPacket):
            raise TypeError('Expected RconPacket type for request')
        if not self.single_packet_mode and multi:
            if not request:
                raise ValueError('Must specify a request packet in order to'
                                 ' read a multi-packet response')
            response = self._read_multi_response(request)
        else:
            # TODO(bsubei): did not test chat packets for single packet responses.
            response = self._recv_pkt()
            # NOTE(bsubei): chat packets may come at any point. Just store them and continue with the normal response.
            # In this case we recursively call read_response until we get a non-chat packet.
            if response.pkt_type == SQUAD_CHAT_STREAM:
                self.chat_messages.append(response.body.decode('utf-8'))
                return self.read_response(request)
        if (not self.single_packet_mode and
                response.pkt_type not in (SERVERDATA_RESPONSE_VALUE, SERVERDATA_AUTH_RESPONSE)):
            raise RconError('Recieved unexpected RCON packet type')
        if request and response.pkt_id != request.pkt_id:
            raise RconError('Response ID does not match request ID')
        return response

    def _read_multi_response(self, req_pkt):
        """Return concatenated multi-packet response."""
        chk_pkt = RconPacket(next(self.pkt_id), SERVERDATA_RESPONSE_VALUE)
        self._send_pkt(chk_pkt)
        # According to the Valve wiki, a server will mirror a
        # SERVERDATA_RESPONSE_VALUE packet and then send an additional response
        # packet with an empty body. So we should concatenate any packets until
        # we receive a response that matches the ID in chk_pkt
        body_parts = []
        # TODO(bsubei): different messages may have different encodings (ascii vs utf-8) based on the kinds of player
        # names for commands like 'ListPlayers'. Keep an eye out for that bug.
        while True:
            response = self._recv_pkt()
            # NOTE(bsubei): chat packets may come at any point. Just store them and continue with the normal response.
            if response.pkt_type == SQUAD_CHAT_STREAM:
                self.chat_messages.append(response.body.decode('utf-8'))
                continue
            if response.pkt_type != SERVERDATA_RESPONSE_VALUE:
                raise RconError('Received unexpected RCON packet type')
            if response.pkt_id == chk_pkt.pkt_id:
                break
            elif response.pkt_id != req_pkt.pkt_id:
                raise RconError('Response ID does not match request ID')
            body_parts.append(response.body)
        # NOTE(bsubei): for Squad servers, end of multipacket is signalled by an empty body response and a special
        # 7-byte packet.
        empty_response = self._recv_pkt()
        if empty_response.pkt_type != SERVERDATA_RESPONSE_VALUE and empty_response.pkt_id != response.pkt_id:
            raise RconError('Expected empty response after multipacket')
        end_of_multipacket = self._recv_pkt()
        if end_of_multipacket.pkt_type != END_OF_MULTIPACKET:
            raise RconError('Expected end-of-multipacket response not received!')

        # Return the packet.
        return RconPacket(req_pkt.pkt_id, SERVERDATA_RESPONSE_VALUE,
                          ''.join(str(body_parts)))

    def get_player_chat(self):
        """ Returns the stored chat messages. """
        return self.chat_messages

    def get_parsed_player_chat(self):
        """
        Parses the stored player chat into a dict of steam IDs mapping to a list of their messages in chronological
        order.

        :return: dict(str->list(str)) The parsed mapping between player IDs and the list of player chat messages.
        """
        player_messages = dict()
        STEAM_ID_PATTERN = r'\[SteamID:(\w*)\]'
        for msg in self.get_player_chat():
            # TODO catch any regex fails and continue
            text = msg.strip('\x00')
            player_id = re.search(STEAM_ID_PATTERN, text).group(1)
            if player_id in player_messages:
                player_messages[player_id].append(text)
            else:
                player_messages.update({player_id: [text]})
        return player_messages

    def clear_player_chat(self):
        """ Clears the chat messages (becomes an empty list). """
        self.chat_messages = []


class RconError(Exception):
    """Generic RCON error."""
    pass


class RconAuthError(RconError):
    """Raised if an RCON Authentication error occurs."""
    pass


class RconSizeError(RconError):
    """Raised when an RCON packet is an illegal size."""
    pass
