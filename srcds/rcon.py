# Copyright (C) 2013 Peter Rowlands
"""Source server RCON communications module"""

import contextlib
import itertools
import logging
import re
import struct
import socket

logger = logging.getLogger(__name__)

# "Vanilla" RCON Packet types
SERVERDATA_AUTH = 3
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_RESPONSE_VALUE = 0
# Special packet type used by Squad RCON servers to indicate a chat message that streams in even without requests.
SQUAD_CHAT_STREAM = 1
# NOTE(bsubei): I completely invented this type just to internally signal end of multipacket response.
END_OF_MULTIPACKET = 77

# Special responses by the Squad server to indicate the end of a multipacket response.
SPECIAL_MULTIPACKET_HEADER = b'\x00\x01\x00\x00\x00\x00\x00'
SPECIAL_MULTIPACKET_BYTES = b'\x00\x00\x00\x01\x00\x00\x00'

# Use these strings for when we're unable to parse the regex for the Steam ID and player name in chat messages.
UNKNOWN_PLAYER_ID = 'Unknown Player ID'
UNKNOWN_PLAYER_NAME = 'Unknown Player Name'


class PlayerChat(object):
    """Represents chat messages from a player"""

    def __init__(self, player_id, player_name, messages=[]):
        self.player_id = player_id
        self.player_name = player_name
        self.messages = messages

    def __str__(self):
        return f'ID: {self.player_id}, name: {self.player_name}, messages: {self.messages}'


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


@contextlib.contextmanager
def get_managed_rcon_connection(*args, **kwargs):
    """ Yields a managed RconConnection (closes its socket when leaving context). """
    conn = RconConnection(*args, **kwargs)
    yield conn
    conn._sock.close()


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
        self.all_player_chat = dict()
        self._authenticate(password)

    def __enter__(self):
        print('enter method called')
        return self

    def __exit__(self, exc_type, exc_value, exc_traceback):
        print('exit method called')

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

        # We got a weird packet here! If it's the special multipacket header, there is nothing left to read for
        # this packet. Otherwise, it's a malformed packet header.
        if header == SPECIAL_MULTIPACKET_HEADER:
            return RconPacket(-1, END_OF_MULTIPACKET, '')
        if len(header) != HEADER_SIZE:
            raise RconError('Received malformed packet header!')

        # Use the given packet size to read the body of the packet.
        (pkt_size, pkt_id, pkt_type) = struct.unpack('<3i', header)
        body = self._sock.recv(pkt_size - 8)

        # NOTE(bsubei): chat packets may come at any point. Just store them and continue with the normal response.
        # In this case we recursively call _recv_pkt until we get a non-chat packet.
        if pkt_type == SQUAD_CHAT_STREAM:
            self.add_chat_message(body.strip(b'\x00\x01').decode('utf-8'))
            return self._recv_pkt()
        # NOTE(bsubei): sometimes the end of multipacket response comes attached with the chat message (and the chat
        # message has the wrong packet type). This was observed on Squad version b-17.0.13.23847.
        # e.g. packet body: b'\x00\x00\x00\x01\x00\x00\x00[ChatAll] this is example chat\x00\x00'.
        if SPECIAL_MULTIPACKET_BYTES in body:
            logger.warning('Found multipacket response inside a chat message! Using chat and discarding the rest!')
            self.add_chat_message(body.strip(b'\x00\x01').decode('utf-8'))
            return RconPacket(-1, END_OF_MULTIPACKET, '')

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
            response = self._recv_pkt()
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
            if response.pkt_type != SERVERDATA_RESPONSE_VALUE:
                raise RconError('Received unexpected RCON packet type')
            if response.pkt_id == chk_pkt.pkt_id:
                break
            elif response.pkt_id != req_pkt.pkt_id:
                raise RconError('Response ID does not match request ID')
            body_parts.append(response.body)
        # NOTE(bsubei): for Squad servers, end of multipacket is signalled by an empty body response and a special
        # 7-byte packet (sometimes included with the next chat message).
        empty_response = self._recv_pkt()
        if empty_response.pkt_type != SERVERDATA_RESPONSE_VALUE and empty_response.pkt_id != response.pkt_id:
            raise RconError('Expected empty response after multipacket')
        end_of_multipacket = self._recv_pkt()
        if (end_of_multipacket.pkt_type != END_OF_MULTIPACKET and
                SPECIAL_MULTIPACKET_BYTES not in end_of_multipacket.body):
            raise RconError('Expected end-of-multipacket response not received!')

        # Return the packet.
        return RconPacket(req_pkt.pkt_id, SERVERDATA_RESPONSE_VALUE,
                          ''.join(str(body_parts)))

    def get_player_chat(self):
        """ Returns the stored player chat objects. """
        return self.all_player_chat

    def add_chat_message(self, chat_message):
        """
        Parses the incoming chat message and adds it to the player chat dict, mapping Steam IDs to a list of their
        player chat objects. (The messages are added in chronological order).

        :return: dict(str->PlayerChat) The parsed mapping between player IDs and the player chat objects.
        """
        # An example chat message:
        # '[ChatAll] [SteamID:12345678901234567] [FP] Clan Member 1 : Hello world! '

        # Use the SteamID regex as the player_id key. If regex fails, use an 'unknown' player id and move on.
        STEAM_ID_PATTERN = r'\[SteamID:(\w*)\]'
        try:
            text = chat_message.strip('\x00')
            player_id = re.search(STEAM_ID_PATTERN, text).group(1)
        except Exception:
            player_id = UNKNOWN_PLAYER_ID

        # Use this regex to get the player_name. Use unknown name if failed.
        PLAYER_NAME_PATTERN = r'\[SteamID:\w*\](.*):'
        try:
            text = chat_message.strip('\x00')
            player_name = re.search(PLAYER_NAME_PATTERN, text).group(1)
        except Exception:
            player_name = UNKNOWN_PLAYER_NAME

        # If the dict has this key before, just append a new message to the list of messages belonging to that key.
        if player_id in self.all_player_chat:
            self.all_player_chat[player_id].all_player_chat.append(text)
        else:
            # Add the player id and the message as a list if it's the first time we see it.
            self.all_player_chat.update({player_id: PlayerChat(player_id, player_name, [text])})

        # Return the chat messages.
        return self.all_player_chat

    def clear_player_chat(self):
        """ Clears the player chat objects (becomes an empty dict). """
        self.all_player_chat = dict()

    def get_current_and_next_map(self):
        """ Returns the current and next maps by querying the RCON server and parsing the response using regex. """
        response = self.exec_command('ShowNextMap')
        current_map, next_map = (None, None)
        try:
            current_map = re.search(r'Current map is (.+),', response).group(1)
            next_map = re.search(r", Next map is (.+)\\x00\\x00']", response).group(1)
        except AttributeError as e:
            logger.error(f'Failed to parse ShowNextMap {current_map}, {next_map}: {e}')
        finally:
            return current_map, next_map

    def get_all_player_ids(self):
        """
        Requests the list of players from the RCON server and returns the list of active player IDs as a list(str).
        """
        # An example response for ListPlayers (note the weird newline chars and the \\x00 bytes):
        # "[b'----- Active Players -----\\n
        # ID: 2 | SteamID: 01234567890123456 | Name: [FP] Clan Member 1 | Team ID: 2 | Squad ID: N/A\\n
        # ID: 0 | SteamID: 01234567890123456 | Name: [FP] Clan Member 2 | Team ID: 1 | Squad ID: N/A\\n
        # ID: 3 | SteamID: 01234567890123456 | Name: some lame rando | Team ID: 2 | Squad ID: N/A\\n
        # ----- Recently Disconnected Players [Max of 15] -----\\n
        # ID: 7 | SteamID: 01234567890123456 | Since Disconnect: 04m.11s | Name: Some Bored Dude\\x00\\x00']"
        response = self.exec_command(f'ListPlayers')
        # Removes the first part ('Active Players' header).
        parsed_response = response.split('\\n')[1:]
        # Removes the last part ('Recently Disconnected Players' section).
        index = next(
            (i for (i, x) in enumerate(parsed_response) if 'Recently Disconnected Players' in x),
            len(parsed_response)
            )
        parsed_response = parsed_response[:index]
        # Now split the response into just the player ids.
        return [r.split('|')[1].split('SteamID:')[-1].strip() for r in parsed_response]


class RconError(Exception):
    """Generic RCON error."""
    pass


class RconAuthError(RconError):
    """Raised if an RCON Authentication error occurs."""
    pass


class RconSizeError(RconError):
    """Raised when an RCON packet is an illegal size."""
    pass
