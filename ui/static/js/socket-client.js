/**
 * socket-client.js — Chkobba SocketIO client wrapper (Phase 3)
 *
 * Exposes:
 *   window.Game          — singleton; call Game.init(roomId, mySeat) on DOMContentLoaded
 *   window.cardStrToData — converts a card display string ("7♦") to server data {suit, rank}
 *
 * The client wires into the existing AJAX engine via window._gameEngine which
 * the game IIFE exposes at the bottom of index.html.
 */
(function () {
  'use strict';

  // ------------------------------------------------------------------
  // Card string ↔ data conversion
  // Card strings are "rank-label + suit-symbol", e.g. "7♦", "K♠", "A♥"
  // ------------------------------------------------------------------
  var SUIT_TO_NAME = { '♦': 'DENARI', '♥': 'CUPS', '♠': 'SWORDS', '♣': 'CLUBS' };
  // Rank labels from engine/utils.py: A 2 3 4 5 6 7 J Q K
  // JACK=9→"J", KNIGHT=8→"Q", KING=10→"K"
  var RANK_TO_VAL  = { 'A': 1, '2': 2, '3': 3, '4': 4, '5': 5,
                       '6': 6, '7': 7, 'J': 9, 'Q': 8, 'K': 10 };

  function cardStrToData(str) {
    if (!str || str.length < 2) return null;
    var sym  = str.slice(-1);
    var rnk  = str.slice(0, -1);
    var suit = SUIT_TO_NAME[sym];
    var rank = RANK_TO_VAL[rnk];
    if (!suit || rank === undefined) return null;
    return { suit: suit, rank: rank };
  }

  window.cardStrToData = cardStrToData;

  // ------------------------------------------------------------------
  // Game singleton
  // ------------------------------------------------------------------
  var Game = {
    socket:   null,
    roomId:   null,
    mySeat:   null,
    lastSeq:  0,
    handlers: {},

    init: function (roomId, mySeat) {
      this.roomId  = roomId;
      this.mySeat  = mySeat;
      this.socket  = io({ transports: ['websocket', 'polling'] });
      this._wireUp();
    },

    _wireUp: function () {
      var self = this;
      var hasConnected = false;

      this.socket.on('connect', function () {
        // Reset delta dedup on reconnect so post-rejoin events are not dropped.
        if (hasConnected) self.lastSeq = 0;
        hasConnected = true;

        self._setStatus('connected');
        self.socket.emit('game_join', { room_id: self.roomId });
      });

      this.socket.on('disconnect', function () {
        self._setStatus('disconnected');
      });

      // socket.io v4 fires 'reconnect' on the Manager, not the Socket.
      // Use 'connect' (fires after every successful connection, including reconnects).
      // The join above re-registers the room each time.

      // ------------------------------------------------------------------
      // state_snapshot — authoritative full state from server
      // ------------------------------------------------------------------
      this.socket.on('state_snapshot', function (data) {
        var eng = window._gameEngine;
        if (!eng) return;
        try {

        // Save the previous state BEFORE overwriting it, so applyState can
        // diff old vs new table slots for the capture-fly animation.
        var prevState = eng.getState ? eng.getState() : null;

        eng.setState(data);
        self.lastSeq = data.seq || 0;

        // During deal / bot capture animation the init path owns card-zone DOM.
        if ((eng.isDealing && eng.isDealing()) || (eng.isBotAnimating && eng.isBotAnimating())) {
          eng.applyState(data, null, prevState);
          return;
        }

        var pred = eng._lastPredicted;
        if (pred) {
          eng._lastPredicted = null;
          // Prediction was applied optimistically; check if it matches server.
          if (eng.statesMatch && eng.statesMatch(pred, data)) {
            if (data.commentary_toast && window.showCommentaryToast)
              window.showCommentaryToast(data.commentary_toast);
            // Human play confirmed — update bot-pending UI only (avoid full table rebuild).
            if (data.has_pending_bot) {
              if (eng.onServerConfirm) {
                eng.onServerConfirm(data);
              } else {
                eng.applyState(data, null, prevState);
              }
              return;
            }
            if (data.round_over || data.match_over) {
              eng.applyState(data, null, prevState);
            }
            return;
          } else {
            // Mismatch — let reconcile() do a brief fade and re-render.
            if (eng.reconcile) { eng.reconcile(data); return; }
          }
        }

        // No prediction pending — apply state directly, passing the previous
        // state so the capture animation can compare old vs new table cards.
        eng.applyState(data, null, prevState);
        } finally {
          if (eng.clearPlayBusy) eng.clearPlayBusy();
        }
      });

      // ------------------------------------------------------------------
      // Delta events — used for animation hints; seq-deduped
      // ------------------------------------------------------------------
      var deltaEvents = [
        'card_played', 'cards_captured', 'score_updated', 'turn_changed',
        'hand_dealt', 'hand_dealt_private', 'round_over', 'match_over',
        'bot_thinking',
        // Multiplayer room events
        'game_start', 'room_player_joined', 'match_starting',
        'waiting_state', 'room_closed', 'chat_message',
        // Disconnection handling
        'player_disconnected', 'player_reconnected',
        'player_replaced_with_bot', 'you_were_replaced',
        // Rematch flow
        'rematch_offered', 'rematch_offered_sent',
        'rematch_accepted', 'rematch_declined',
        // Matchmaking
        'matchmaking_status',
        'player_profile_updated',
        'friend_request_received',
        'friend_request_resolved',
        'play_invite_received',
        'play_invite_resolved',
      ];
      deltaEvents.forEach(function (evt) {
        self.socket.on(evt, function (data) {
          if (data.seq && data.seq <= self.lastSeq) return; // dedupe
          if (data.seq) self.lastSeq = data.seq;
          if (self.handlers[evt]) self.handlers[evt](data);
        });
      });

      this.socket.on('error', function (data) {
        console.warn('[socket] server error:', (data || {}).message);
      });
    },

    // Register a handler for a specific delta event
    on: function (name, fn) { this.handlers[name] = fn; return this; },

    // Emit a play_card event to the server
    playCard: function (card, captures) {
      if (!this.socket || !this.socket.connected) return;
      this.socket.emit('play_card', {
        room_id:  this.roomId,
        card:     card,
        captures: captures || []
      });
    },

    _setStatus: function (status) {
      var dot = document.querySelector('.conn-dot');
      if (dot) dot.className = 'conn-dot ' + status;
      var label = document.getElementById('conn-status-text');
      if (label) {
        label.textContent = status === 'connected' ? 'Connected' : 'Disconnected';
      }
    }
  };

  window.Game = Game;
})();
