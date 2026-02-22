/**
 * Node.js Voice Bridge for DAVE E2EE Support
 *
 * This bridge handles Discord voice connections with full DAVE protocol
 * support via @discordjs/voice. It communicates with the Python bot
 * over WebSocket, receiving voice credentials and sending/receiving
 * audio as base64-encoded PCM.
 *
 * Architecture:
 *   Python bot (gateway owner) -- WebSocket --> Node bridge (voice I/O)
 *   Python sends: voice_server_update, voice_state_update, play commands
 *   Node sends: decoded PCM audio per user, ready/error/disconnect events
 */

const { WebSocketServer, WebSocket } = require('ws');
const {
  joinVoiceChannel,
  createAudioPlayer,
  createAudioResource,
  AudioPlayerStatus,
  VoiceConnectionStatus,
  EndBehaviorType,
  NoSubscriberBehavior,
  StreamType,
  entersState,
} = require('@discordjs/voice');
const { PassThrough, Readable } = require('stream');
const { OpusEncoder } = require('@discordjs/opus');

const PORT = parseInt(process.env.BRIDGE_PORT || '9876', 10);
const LOG_LEVEL = process.env.LOG_LEVEL || 'INFO';

// Simple logger
const LOG_LEVELS = { DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3 };
const currentLevel = LOG_LEVELS[LOG_LEVEL.toUpperCase()] ?? LOG_LEVELS.INFO;

function log(level, msg, ...args) {
  if ((LOG_LEVELS[level] ?? 0) >= currentLevel) {
    const ts = new Date().toISOString();
    console.log(`${ts} [${level}] voice-bridge: ${msg}`, ...args);
  }
}

/**
 * Manages voice connections for a single guild.
 * Uses a custom adapter to receive voice credentials from the Python bot
 * instead of connecting to the Discord gateway directly.
 */
class GuildVoiceConnection {
  constructor(guildId, channelId, userId, sessionId, send) {
    this.guildId = guildId;
    this.channelId = channelId;
    this.userId = userId;
    this.sessionId = sessionId;
    this.send = send; // function to send messages back to Python
    this.connection = null;
    this.player = null;
    this.receiver = null;
    this.listeningStreams = new Map(); // ssrc -> stream
    this.userSsrcMap = new Map(); // ssrc -> userId

    // Adapter methods will be set when creating the connection
    this._adapterMethods = null;
  }

  /**
   * Create the voice connection using a custom adapter.
   * The adapter receives voice credentials forwarded from the Python bot
   * rather than reading them from a gateway connection.
   */
  connect() {
    log('INFO', `Connecting to voice: guild=${this.guildId} channel=${this.channelId}`);

    const self = this;

    this.connection = joinVoiceChannel({
      channelId: this.channelId,
      guildId: this.guildId,
      selfDeaf: false,
      selfMute: false,
      adapterCreator: (methods) => {
        // Store adapter methods so we can feed voice data from Python
        self._adapterMethods = methods;
        return {
          sendPayload: (payload) => {
            // The Python bot already sent the gateway opcode 4 (voice state update).
            // We don't need to send anything to the gateway -- just return true
            // to indicate success so @discordjs/voice proceeds with the connection.
            log('DEBUG', `Adapter sendPayload (no-op): op=${payload.op}`);
            return true;
          },
          destroy: () => {
            log('DEBUG', 'Adapter destroyed');
            self._adapterMethods = null;
          },
        };
      },
    });

    // Create audio player
    this.player = createAudioPlayer({
      behaviors: {
        noSubscriber: NoSubscriberBehavior.Play,
      },
    });
    this.connection.subscribe(this.player);

    // Set up connection event handlers
    this.connection.on(VoiceConnectionStatus.Ready, () => {
      log('INFO', `Voice connection ready: guild=${this.guildId}`);
      this.send({
        op: 'ready',
        guild_id: this.guildId,
        dave: true,
      });
      this._startListening();
    });

    this.connection.on(VoiceConnectionStatus.Disconnected, async () => {
      log('WARNING', `Voice disconnected: guild=${this.guildId}`);
      try {
        // Try to reconnect within 5 seconds
        await Promise.race([
          entersState(this.connection, VoiceConnectionStatus.Signalling, 5000),
          entersState(this.connection, VoiceConnectionStatus.Connecting, 5000),
        ]);
        log('INFO', `Voice reconnecting: guild=${this.guildId}`);
      } catch {
        // Couldn't reconnect, destroy
        log('WARNING', `Voice connection lost, destroying: guild=${this.guildId}`);
        this.destroy();
        this.send({ op: 'disconnected', guild_id: this.guildId });
      }
    });

    this.connection.on(VoiceConnectionStatus.Destroyed, () => {
      log('INFO', `Voice connection destroyed: guild=${this.guildId}`);
    });

    this.connection.on('error', (err) => {
      log('ERROR', `Voice connection error: guild=${this.guildId}`, err.message);
      this.send({ op: 'error', guild_id: this.guildId, message: err.message });
    });

    this.player.on('error', (err) => {
      log('ERROR', `Audio player error: guild=${this.guildId}`, err.message);
    });

    this.player.on(AudioPlayerStatus.Idle, () => {
      log('DEBUG', `Playback finished: guild=${this.guildId}`);
      this.send({ op: 'play_done', guild_id: this.guildId });
    });

    return this.connection;
  }

  /**
   * Feed voice server update data from the Python bot into the adapter.
   */
  onVoiceServerUpdate(data) {
    if (!this._adapterMethods) {
      log('WARNING', 'No adapter methods available for voice_server_update');
      return;
    }
    log('DEBUG', `Feeding voice_server_update: guild=${this.guildId} endpoint=${data.endpoint}`);
    this._adapterMethods.onVoiceServerUpdate({
      token: data.token,
      guild_id: data.guild_id,
      endpoint: data.endpoint,
    });
  }

  /**
   * Feed voice state update data from the Python bot into the adapter.
   */
  onVoiceStateUpdate(data) {
    if (!this._adapterMethods) {
      log('WARNING', 'No adapter methods available for voice_state_update');
      return;
    }
    log('DEBUG', `Feeding voice_state_update: guild=${this.guildId} session=${data.session_id}`);
    this._adapterMethods.onVoiceStateUpdate({
      channel_id: data.channel_id,
      guild_id: data.guild_id,
      user_id: data.user_id,
      session_id: data.session_id,
    });
  }

  /**
   * Start listening to incoming audio from all users in the voice channel.
   * Decodes Opus to PCM and sends to Python as base64.
   */
  _startListening() {
    if (!this.connection) return;

    this.receiver = this.connection.receiver;

    this.receiver.speaking.on('start', (userId) => {
      if (this.listeningStreams.has(userId)) return;

      log('DEBUG', `User started speaking: ${userId}`);

      const opusStream = this.receiver.subscribe(userId, {
        end: {
          behavior: EndBehaviorType.AfterSilence,
          duration: 1000,
        },
      });

      // Decode opus to PCM (48kHz, stereo, 16-bit)
      const encoder = new OpusEncoder(48000, 2);
      const pcmChunks = [];

      opusStream.on('data', (chunk) => {
        try {
          const pcm = encoder.decode(chunk);
          pcmChunks.push(pcm);
        } catch (err) {
          log('DEBUG', `Opus decode error for user ${userId}: ${err.message}`);
        }
      });

      opusStream.on('end', () => {
        this.listeningStreams.delete(userId);

        if (pcmChunks.length === 0) return;

        const fullPcm = Buffer.concat(pcmChunks);
        log('DEBUG', `User ${userId} speech ended: ${fullPcm.length} bytes PCM`);

        // Send PCM audio to Python as base64
        // Format: 48kHz, stereo, 16-bit signed LE
        this.send({
          op: 'audio',
          user_id: userId,
          guild_id: this.guildId,
          pcm: fullPcm.toString('base64'),
        });
      });

      opusStream.on('error', (err) => {
        log('ERROR', `Opus stream error for user ${userId}: ${err.message}`);
        this.listeningStreams.delete(userId);
      });

      this.listeningStreams.set(userId, opusStream);
    });
  }

  /**
   * Play audio in the voice channel.
   * Accepts base64-encoded WAV data.
   */
  play(audioBase64, format = 'wav') {
    if (!this.player) {
      log('WARNING', `No player available for guild=${this.guildId}`);
      return;
    }

    const buffer = Buffer.from(audioBase64, 'base64');
    log('DEBUG', `Playing audio: guild=${this.guildId}, ${buffer.length} bytes, format=${format}`);

    const stream = new PassThrough();
    stream.end(buffer);

    let resource;
    if (format === 'pcm') {
      resource = createAudioResource(stream, {
        inputType: StreamType.Raw,
        inlineVolume: false,
      });
    } else {
      // WAV, MP3, etc. -- let FFmpeg handle it
      resource = createAudioResource(stream, {
        inputType: StreamType.Arbitrary,
      });
    }

    this.player.play(resource);
  }

  /**
   * Stop current playback.
   */
  stop() {
    if (this.player) {
      this.player.stop();
    }
  }

  /**
   * Destroy the voice connection and clean up.
   */
  destroy() {
    for (const [userId, stream] of this.listeningStreams) {
      stream.destroy();
    }
    this.listeningStreams.clear();

    if (this.player) {
      this.player.stop();
    }

    if (this.connection) {
      try {
        this.connection.destroy();
      } catch (err) {
        log('DEBUG', `Error destroying connection: ${err.message}`);
      }
      this.connection = null;
    }
  }
}

/**
 * Main WebSocket server that handles communication with the Python bot.
 */
class VoiceBridge {
  constructor(port) {
    this.port = port;
    this.guilds = new Map(); // guildId -> GuildVoiceConnection
    this.ws = null;
    this.wss = null;
  }

  start() {
    this.wss = new WebSocketServer({ port: this.port });
    log('INFO', `Voice bridge WebSocket server listening on port ${this.port}`);

    this.wss.on('connection', (ws) => {
      log('INFO', 'Python bot connected');
      this.ws = ws;

      ws.on('message', (raw) => {
        try {
          const msg = JSON.parse(raw.toString());
          this._handleMessage(msg);
        } catch (err) {
          log('ERROR', `Failed to parse message: ${err.message}`);
        }
      });

      ws.on('close', () => {
        log('WARNING', 'Python bot disconnected');
        this.ws = null;
        // Destroy all voice connections
        for (const [guildId, conn] of this.guilds) {
          conn.destroy();
        }
        this.guilds.clear();
      });

      ws.on('error', (err) => {
        log('ERROR', `WebSocket error: ${err.message}`);
      });
    });
  }

  _send(msg) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    }
  }

  _handleMessage(msg) {
    const { op } = msg;
    log('DEBUG', `Received op: ${op}`, op === 'play' ? '(audio data omitted)' : JSON.stringify(msg).slice(0, 200));

    switch (op) {
      case 'join':
        this._handleJoin(msg);
        break;
      case 'voice_state_update':
        this._handleVoiceStateUpdate(msg);
        break;
      case 'voice_server_update':
        this._handleVoiceServerUpdate(msg);
        break;
      case 'play':
        this._handlePlay(msg);
        break;
      case 'stop':
        this._handleStop(msg);
        break;
      case 'disconnect':
        this._handleDisconnect(msg);
        break;
      default:
        log('WARNING', `Unknown op: ${op}`);
    }
  }

  _handleJoin(msg) {
    const { guild_id, channel_id, user_id, session_id } = msg;

    // Clean up existing connection for this guild
    if (this.guilds.has(guild_id)) {
      this.guilds.get(guild_id).destroy();
      this.guilds.delete(guild_id);
    }

    const conn = new GuildVoiceConnection(
      guild_id,
      channel_id,
      user_id,
      session_id,
      (m) => this._send(m),
    );
    this.guilds.set(guild_id, conn);
    conn.connect();
  }

  _handleVoiceStateUpdate(msg) {
    const guildId = msg.d.guild_id;
    const conn = this.guilds.get(guildId);
    if (!conn) {
      log('WARNING', `No connection for guild ${guildId} (voice_state_update)`);
      return;
    }
    conn.onVoiceStateUpdate(msg.d);
  }

  _handleVoiceServerUpdate(msg) {
    const guildId = msg.d.guild_id;
    const conn = this.guilds.get(guildId);
    if (!conn) {
      log('WARNING', `No connection for guild ${guildId} (voice_server_update)`);
      return;
    }
    conn.onVoiceServerUpdate(msg.d);
  }

  _handlePlay(msg) {
    const { guild_id, audio, format } = msg;
    const conn = this.guilds.get(guild_id);
    if (!conn) {
      log('WARNING', `No connection for guild ${guild_id} (play)`);
      return;
    }
    conn.play(audio, format || 'wav');
  }

  _handleStop(msg) {
    const { guild_id } = msg;
    const conn = this.guilds.get(guild_id);
    if (conn) conn.stop();
  }

  _handleDisconnect(msg) {
    const { guild_id } = msg;
    const conn = this.guilds.get(guild_id);
    if (conn) {
      conn.destroy();
      this.guilds.delete(guild_id);
    }
  }
}

// Start the bridge
const bridge = new VoiceBridge(PORT);
bridge.start();

// Graceful shutdown
process.on('SIGINT', () => {
  log('INFO', 'Shutting down...');
  for (const [, conn] of bridge.guilds) {
    conn.destroy();
  }
  bridge.wss?.close();
  process.exit(0);
});

process.on('SIGTERM', () => {
  log('INFO', 'Shutting down...');
  for (const [, conn] of bridge.guilds) {
    conn.destroy();
  }
  bridge.wss?.close();
  process.exit(0);
});
