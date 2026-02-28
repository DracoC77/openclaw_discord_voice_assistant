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

const http = require('http');
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
const HEALTH_PORT = parseInt(process.env.HEALTH_PORT || '9877', 10);
const LOG_LEVEL = process.env.LOG_LEVEL || 'INFO';

// Number of opus frames (~20ms each) to accumulate before checking RMS
// for early barge-in detection.  3 frames = ~60ms of audio.
const BARGE_IN_FRAME_COUNT = 3;
// RMS threshold for early barge-in detection during playback.
// Must match PLAYBACK_SPEECH_THRESHOLD on the Python side.
const BARGE_IN_RMS_THRESHOLD = 1200;

// Silence duration before the bridge considers a user's speech finished.
// Shorter = more responsive but risks splitting mid-sentence pauses.
const BRIDGE_SILENCE_MS = parseInt(process.env.BRIDGE_SILENCE_MS || '600', 10);
// Even shorter timeout when the bot is playing audio, since barge-in
// speech is typically short commands ("stop", "hold on").
const BRIDGE_SILENCE_PLAYBACK_MS = parseInt(process.env.BRIDGE_SILENCE_PLAYBACK_MS || '300', 10);

// Fade-out duration in milliseconds when stopping playback for barge-in.
const FADE_DURATION_MS = 100;
const FADE_STEPS = 5;
const FADE_INTERVAL_MS = FADE_DURATION_MS / FADE_STEPS;

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
 * Compute RMS (root mean square) of 16-bit PCM audio in a Buffer.
 * Used for early barge-in detection during playback.
 */
function computeRMS(buffer) {
  const samples = new Int16Array(
    buffer.buffer,
    buffer.byteOffset,
    Math.floor(buffer.length / 2),
  );
  if (samples.length === 0) return 0;
  let sumSquares = 0;
  for (let i = 0; i < samples.length; i++) {
    sumSquares += samples[i] * samples[i];
  }
  return Math.sqrt(sumSquares / samples.length);
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
    this.listeningStreams = new Map(); // userId -> stream
    this.userSsrcMap = new Map(); // ssrc -> userId

    // Adapter methods will be set when creating the connection
    this._adapterMethods = null;
    // Queue for voice credentials arriving before adapter is ready
    this._pendingUpdates = [];

    // Looping playback state (used for thinking sound)
    this._loopAudio = null;   // base64 audio to loop, or null
    this._loopFormat = null;  // format of the looped audio

    // Current AudioResource reference (for volume fade-out)
    this._currentResource = null;
    this._fadeTimer = null;
    this._errorRecoveryTimer = null;
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

        // Replay any queued updates that arrived before adapter was ready
        for (const pending of self._pendingUpdates) {
          if (pending.type === 'voice_server_update') {
            methods.onVoiceServerUpdate(pending.data);
          } else if (pending.type === 'voice_state_update') {
            methods.onVoiceStateUpdate(pending.data);
          }
        }
        self._pendingUpdates = [];

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
      // Cancel any error recovery timer — the connection recovered
      if (this._errorRecoveryTimer) {
        clearTimeout(this._errorRecoveryTimer);
        this._errorRecoveryTimer = null;
      }
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

      // If the Disconnected handler doesn't fire (e.g. 521 errors that
      // leave the connection in a stuck state), fall back to destroying
      // the connection after a timeout.  If new voice credentials arrive
      // from Python (via voice_server_update forwarding) the adapter will
      // handle reconnection before this fires.
      if (this._errorRecoveryTimer) clearTimeout(this._errorRecoveryTimer);
      this._errorRecoveryTimer = setTimeout(() => {
        this._errorRecoveryTimer = null;
        if (
          this.connection &&
          this.connection.state.status !== VoiceConnectionStatus.Ready
        ) {
          log('WARNING', `Voice connection failed to recover after error, destroying: guild=${this.guildId}`);
          this.destroy();
          this.send({ op: 'disconnected', guild_id: this.guildId });
        }
      }, 10_000);
    });

    this.player.on('error', (err) => {
      log('ERROR', `Audio player error: guild=${this.guildId}`, err.message);
    });

    this.player.on(AudioPlayerStatus.Idle, () => {
      if (this._loopAudio) {
        log('DEBUG', `Looping audio: guild=${this.guildId}`);
        this.play(this._loopAudio, this._loopFormat || 'wav');
      } else {
        log('DEBUG', `Playback finished: guild=${this.guildId}`);
        this.send({ op: 'play_done', guild_id: this.guildId });
      }
    });

    return this.connection;
  }

  /**
   * Feed voice server update data from the Python bot into the adapter.
   * If the adapter isn't ready yet, queues the update for replay.
   */
  onVoiceServerUpdate(data) {
    if (!this._adapterMethods) {
      log('INFO', 'Adapter not ready, queuing voice_server_update');
      this._pendingUpdates.push({ type: 'voice_server_update', data });
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
   * If the adapter isn't ready yet, queues the update for replay.
   */
  onVoiceStateUpdate(data) {
    if (!this._adapterMethods) {
      log('INFO', 'Adapter not ready, queuing voice_state_update');
      this._pendingUpdates.push({ type: 'voice_state_update', data });
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

      // Use shorter silence timeout during playback for faster barge-in
      const isPlaying = this.player && this.player.state.status === AudioPlayerStatus.Playing;
      const silenceDuration = isPlaying ? BRIDGE_SILENCE_PLAYBACK_MS : BRIDGE_SILENCE_MS;
      // Tag the segment so Python knows whether playback was active when
      // capture started.  This lets Python deterministically filter stale
      // echo/crosstalk without relying on timing heuristics.
      const capturedDuringPlayback = isPlaying;
      log('DEBUG', `User started speaking: ${userId} (silence timeout: ${silenceDuration}ms, during_playback: ${capturedDuringPlayback})`);

      const opusStream = this.receiver.subscribe(userId, {
        end: {
          behavior: EndBehaviorType.AfterSilence,
          duration: silenceDuration,
        },
      });

      // Store the stream reference BEFORE attaching event listeners
      // to prevent leaks if an error occurs during setup.
      this.listeningStreams.set(userId, opusStream);

      // Decode opus to PCM (48kHz, stereo, 16-bit)
      const encoder = new OpusEncoder(48000, 2);
      const pcmChunks = [];
      let bargeInSent = false;

      opusStream.on('data', (chunk) => {
        try {
          const pcm = encoder.decode(chunk);
          pcmChunks.push(pcm);

          // Early barge-in detection: after a few frames (~60ms), if the
          // audio player is currently playing, compute RMS and notify Python
          // immediately so it can interrupt without waiting for the full
          // utterance + 1s silence timeout.
          if (
            !bargeInSent &&
            pcmChunks.length === BARGE_IN_FRAME_COUNT &&
            this.player &&
            this.player.state.status === AudioPlayerStatus.Playing
          ) {
            const combined = Buffer.concat(pcmChunks);
            const rms = computeRMS(combined);
            if (rms > BARGE_IN_RMS_THRESHOLD) {
              bargeInSent = true;
              log('DEBUG', `Early barge-in for user ${userId}: rms=${Math.round(rms)}`);
              this.send({
                op: 'speaking_start',
                user_id: userId,
                guild_id: this.guildId,
                rms: Math.round(rms),
              });
            }
          }
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
          during_playback: capturedDuringPlayback,
        });
      });

      opusStream.on('error', (err) => {
        log('ERROR', `Opus stream error for user ${userId}: ${err.message}`);
        this.listeningStreams.delete(userId);
      });
    });
  }

  /**
   * Play audio in the voice channel.
   * Accepts base64-encoded WAV data.
   */
  play(audioBase64, format = 'wav', loop = false) {
    if (!this.player) {
      log('WARNING', `No player available for guild=${this.guildId}`);
      return;
    }

    // Cancel any ongoing fade from a previous stop
    if (this._fadeTimer) {
      clearInterval(this._fadeTimer);
      this._fadeTimer = null;
    }

    // Store loop state (only set on the initial call, not on re-plays from Idle handler)
    if (loop) {
      this._loopAudio = audioBase64;
      this._loopFormat = format;
    } else if (!this._loopAudio) {
      // Non-looping play clears any previous loop
      this._loopAudio = null;
      this._loopFormat = null;
    }

    const buffer = Buffer.from(audioBase64, 'base64');
    log('DEBUG', `Playing audio: guild=${this.guildId}, ${buffer.length} bytes, format=${format}, loop=${!!this._loopAudio}`);

    const stream = new PassThrough();
    stream.end(buffer);

    let resource;
    if (format === 'pcm') {
      resource = createAudioResource(stream, {
        inputType: StreamType.Raw,
        inlineVolume: true,
      });
    } else {
      // WAV, MP3, etc. -- let FFmpeg handle it
      resource = createAudioResource(stream, {
        inputType: StreamType.Arbitrary,
        inlineVolume: true,
      });
    }

    this._currentResource = resource;
    this.player.play(resource);
  }

  /**
   * Stop current playback.
   * @param {boolean} fade - If true, ramp volume to 0 over ~100ms before stopping.
   */
  stop(fade = false) {
    // Clear loop state first so the Idle handler doesn't restart playback
    this._loopAudio = null;
    this._loopFormat = null;

    // Cancel any in-progress fade
    if (this._fadeTimer) {
      clearInterval(this._fadeTimer);
      this._fadeTimer = null;
    }

    if (fade && this._currentResource && this._currentResource.volume) {
      // Quick fade-out: ramp volume to 0 over ~100ms, then stop
      let step = 0;
      this._fadeTimer = setInterval(() => {
        step++;
        const vol = 1.0 - (step / FADE_STEPS);
        try {
          this._currentResource.volume.setVolume(Math.max(0, vol));
        } catch (_) {
          // Resource may already be destroyed
        }
        if (step >= FADE_STEPS) {
          clearInterval(this._fadeTimer);
          this._fadeTimer = null;
          if (this.player) this.player.stop();
        }
      }, FADE_INTERVAL_MS);
    } else {
      if (this.player) this.player.stop();
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
    this._pendingUpdates = [];
    this._loopAudio = null;
    this._loopFormat = null;
    this._currentResource = null;
    if (this._fadeTimer) {
      clearInterval(this._fadeTimer);
      this._fadeTimer = null;
    }
    if (this._errorRecoveryTimer) {
      clearTimeout(this._errorRecoveryTimer);
      this._errorRecoveryTimer = null;
    }

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
  constructor(port, healthPort) {
    this.port = port;
    this.healthPort = healthPort;
    this.guilds = new Map(); // guildId -> GuildVoiceConnection
    this.ws = null;
    this.wss = null;
    this.healthServer = null;
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

    // Start HTTP health check server
    this.healthServer = http.createServer((req, res) => {
      if (req.url === '/health') {
        const status = {
          status: 'ok',
          ws_connected: this.ws !== null && this.ws.readyState === WebSocket.OPEN,
          active_guilds: this.guilds.size,
        };
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(status));
      } else {
        res.writeHead(404);
        res.end();
      }
    });
    this.healthServer.listen(this.healthPort, () => {
      log('INFO', `Health check server listening on port ${this.healthPort}`);
    });
  }

  _send(msg) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    } else {
      log('WARNING', `Cannot send message (op=${msg.op}): WebSocket not connected`);
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
    const { guild_id, audio, format, loop } = msg;
    const conn = this.guilds.get(guild_id);
    if (!conn) {
      log('WARNING', `No connection for guild ${guild_id} (play)`);
      return;
    }
    conn.play(audio, format || 'wav', !!loop);
  }

  _handleStop(msg) {
    const { guild_id, fade } = msg;
    const conn = this.guilds.get(guild_id);
    if (conn) conn.stop(!!fade);
  }

  _handleDisconnect(msg) {
    const { guild_id } = msg;
    const conn = this.guilds.get(guild_id);
    if (conn) {
      conn.destroy();
      this.guilds.delete(guild_id);
    }
  }

  shutdown() {
    for (const [, conn] of this.guilds) {
      conn.destroy();
    }
    this.guilds.clear();
    this.wss?.close();
    this.healthServer?.close();
  }
}

// Start the bridge
const bridge = new VoiceBridge(PORT, HEALTH_PORT);
bridge.start();

// Graceful shutdown
process.on('SIGINT', () => {
  log('INFO', 'Shutting down...');
  bridge.shutdown();
  process.exit(0);
});

process.on('SIGTERM', () => {
  log('INFO', 'Shutting down...');
  bridge.shutdown();
  process.exit(0);
});
