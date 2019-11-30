Fork
-----
A fork of [`pyscrds`](https://github.com/pmrowla/pysrcds), mainly to add support for [Squad's](https://joinsquad.com/) RCON servers, which use a slightly modified RCON protocol (handles multi-packet responses differently and also sends "chat" packets as a stream as they come in).

NOTE: the chat messages are "flushed" after any attempt to read from the RCON socket (such as sending a command like `ShowNextMap`). The current (hacky) implementation doesn't allow for actively listening to chat. Feel free to change it (e.g. by using non-blocking sockets and multiple threads), but it was just too messy for my taste.

License
-------
The original pysrcds is distributed under the MIT license. This fork uses the same license. See [LICENSE.md](https://github.com/bsubei/pysrcds/blob/master/LICENSE.md) for more information.
