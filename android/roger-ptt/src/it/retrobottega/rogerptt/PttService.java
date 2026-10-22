package it.retrobottega.rogerptt;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioTrack;
import android.media.session.MediaSession;
import android.media.session.PlaybackState;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.util.Log;
import android.view.KeyEvent;

/**
 * Foreground service with an "active" MediaSession: Android delivers the headset's media buttons
 * to the most recent active session, so to us as long as no music player starts.
 * It plays nothing: the PlaybackState.STATE_CONNECTING state is enough to count as active.
 *
 * Buttons: one press opens the microphone, the next one closes it (broadcast "on"/"off" to Mumla,
 * state kept here: more robust than "toggle", which depends on what Mumla believes). Long press =
 * close anyway, to get back in sync. Every press immediately plays a sound in the headset: two
 * rising notes = open, two falling = closed. The sound goes through USAGE_MEDIA like Mumla's audio
 * (A2DP), with a bit of silence in front because idle Bluetooth headsets swallow the attack.
 */
public class PttService extends Service {
    static final String TAG = "RogerPTT";
    static final String ACTION_START = "it.retrobottega.rogerptt.START";
    static final String ACTION_STOP = "it.retrobottega.rogerptt.STOP";
    static final String ACTION_TOGGLE = "it.retrobottega.rogerptt.TOGGLE";
    static final String MUMLA_PACKAGE = "se.lublin.mumla";
    static final String MUMLA_TALK = "se.lublin.mumla.action.TALK";
    static final String CHANNEL = "roger_ptt";
    static final int NOTIFICATION_ID = 1;
    static final long LONG_PRESS_MS = 600;
    static final int TONE_RATE = 16000;

    static volatile boolean running = false;
    static volatile boolean talking = false;

    private MediaSession session;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private long downAt = 0;
    private int toggles = 0;

    @Override public void onCreate() {
        super.onCreate();
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel(CHANNEL, "Roger PTT",
                NotificationManager.IMPORTANCE_LOW));
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_START : intent.getAction();
        if (ACTION_STOP.equals(action)) {
            stopSelf();
            return START_NOT_STICKY;
        }
        if (session == null) {
            startAsForeground();
            openSession();
            running = true;
        }
        if (ACTION_TOGGLE.equals(action)) setTalking(!talking);
        else grabMediaButtons();   // START, even repeated: takes the button back if a player stole it
        return START_STICKY;
    }

    private void startAsForeground() {
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIFICATION_ID, notification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK);
        } else {
            startForeground(NOTIFICATION_ID, notification());
        }
    }

    private Notification notification() {
        Intent toggle = new Intent(this, PttService.class).setAction(ACTION_TOGGLE);
        Intent stop = new Intent(this, PttService.class).setAction(ACTION_STOP);
        int fl = PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE;
        Notification.Builder b = new Notification.Builder(this, CHANNEL)
                .setSmallIcon(R.drawable.ic_mic)
                .setContentTitle("Roger PTT")
                .setContentText((talking ? "MICROPHONE OPEN" : "microphone closed")
                        + " · headset button to switch · " + toggles)
                .setOngoing(true)
                .setContentIntent(PendingIntent.getActivity(this, 0,
                        new Intent(this, MainActivity.class), fl))
                .addAction(new Notification.Action.Builder(null, "Microphone",
                        PendingIntent.getService(this, 1, toggle, fl)).build())
                .addAction(new Notification.Action.Builder(null, "Stop",
                        PendingIntent.getService(this, 2, stop, fl)).build());
        return b.build();
    }

    private void openSession() {
        session = new MediaSession(this, "RogerPTT");
        session.setCallback(new MediaSession.Callback() {
            @Override public boolean onMediaButtonEvent(Intent mediaButtonIntent) {
                KeyEvent ev = mediaButtonIntent.getParcelableExtra(Intent.EXTRA_KEY_EVENT);
                if (ev == null) return super.onMediaButtonEvent(mediaButtonIntent);
                return handleKey(ev);
            }
            // Some headsets arrive as transport commands rather than as KeyEvents
            @Override public void onPlay() { setTalking(!talking); }
            @Override public void onPause() { setTalking(!talking); }
        });
        // Without declared actions the buttons do not arrive; CONNECTING counts as "playing"
        // for button routing, with no audio or focus
        session.setPlaybackState(new PlaybackState.Builder()
                .setActions(PlaybackState.ACTION_PLAY | PlaybackState.ACTION_PAUSE
                        | PlaybackState.ACTION_PLAY_PAUSE | PlaybackState.ACTION_STOP)
                .setState(PlaybackState.STATE_CONNECTING, 0, 1.0f)
                .build());
        session.setActive(true);
        Log.i(TAG, "media session active");
    }

    /**
     * Android delivers media buttons to the session of the app that last played audio, not to
     * the most recent active session: half a second of silence on USAGE_MEDIA is enough to become
     * that app. To be redone if a music player has started in the meantime.
     */
    private void grabMediaButtons() {
        final int rate = 8000;
        final int bytes = rate;   // 0.5 s of 16-bit mono PCM at 8 kHz
        try {
            final AudioTrack track = new AudioTrack.Builder()
                    .setAudioAttributes(new AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_MEDIA)
                            .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build())
                    .setAudioFormat(new AudioFormat.Builder()
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                            .setSampleRate(rate)
                            .setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
                    .setTransferMode(AudioTrack.MODE_STATIC)
                    .setBufferSizeInBytes(bytes)
                    .build();
            track.write(new byte[bytes], 0, bytes);
            track.play();
            handler.postDelayed(() -> { track.stop(); track.release(); }, 900);
            Log.i(TAG, "grabbing media buttons with silence");
        } catch (Exception e) {
            Log.w(TAG, "silence playback failed: " + e);
        }
    }

    private boolean handleKey(KeyEvent ev) {
        int code = ev.getKeyCode();
        boolean ours = code == KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE || code == KeyEvent.KEYCODE_MEDIA_PLAY
                || code == KeyEvent.KEYCODE_MEDIA_PAUSE || code == KeyEvent.KEYCODE_HEADSETHOOK;
        if (!ours) {
            Log.i(TAG, "other media key: " + code);
            return true;   // consumed anyway: we do not want a player to start
        }
        if (ev.getAction() == KeyEvent.ACTION_DOWN) {
            if (ev.getRepeatCount() == 0) downAt = ev.getEventTime();
            return true;
        }
        if (ev.getAction() == KeyEvent.ACTION_UP) {
            long held = ev.getEventTime() - downAt;
            if (downAt > 0 && held >= LONG_PRESS_MS) {
                Log.i(TAG, "long press (" + held + " ms): forcing mic off");
                setTalking(false);
            } else {
                setTalking(!talking);   // right away, without waiting for a possible double press
            }
            downAt = 0;
        }
        return true;
    }

    /** Opens or closes Mumla's microphone and announces it in the headset. */
    private void setTalking(boolean on) {
        Intent i = new Intent(MUMLA_TALK).setPackage(MUMLA_PACKAGE).putExtra("status", on ? "on" : "off");
        sendBroadcast(i);
        talking = on;
        toggles++;
        playTone(on);
        Log.i(TAG, "mic " + (on ? "ON" : "off") + " (" + toggles + ")");
        getSystemService(NotificationManager.class).notify(NOTIFICATION_ID, notification());
    }

    /** Two notes: rising for "open", falling for "closed". 150 ms of silence in front. */
    private void playTone(boolean up) {
        int[] freqs = up ? new int[]{520, 780} : new int[]{780, 520};
        byte[] pcm = pcmTone(150, freqs, 90, 0.35f);
        try {
            final AudioTrack track = new AudioTrack.Builder()
                    .setAudioAttributes(new AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_MEDIA)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build())
                    .setAudioFormat(new AudioFormat.Builder()
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                            .setSampleRate(TONE_RATE)
                            .setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
                    .setTransferMode(AudioTrack.MODE_STATIC)
                    .setBufferSizeInBytes(pcm.length)
                    .build();
            track.write(pcm, 0, pcm.length);
            track.play();
            handler.postDelayed(() -> { track.stop(); track.release(); }, 1200);
        } catch (Exception e) {
            Log.w(TAG, "tone failed: " + e);
        }
    }

    /** 16-bit mono PCM: initial silence, then one note per frequency, with soft attack and release. */
    static byte[] pcmTone(int prerollMs, int[] freqs, int noteMs, float volume) {
        int pre = TONE_RATE * prerollMs / 1000;
        int note = TONE_RATE * noteMs / 1000;
        int fade = TONE_RATE / 100;   // 10 ms
        short[] out = new short[pre + note * freqs.length + TONE_RATE / 20];
        int pos = pre;
        for (int f : freqs) {
            for (int i = 0; i < note; i++) {
                double env = i < fade ? (double) i / fade : (i > note - fade ? (double) (note - i) / fade : 1.0);
                out[pos + i] = (short) (32767 * volume * env * Math.sin(2 * Math.PI * f * i / TONE_RATE));
            }
            pos += note;
        }
        byte[] bytes = new byte[out.length * 2];
        for (int i = 0; i < out.length; i++) {
            bytes[2 * i] = (byte) (out[i] & 0xff);
            bytes[2 * i + 1] = (byte) ((out[i] >> 8) & 0xff);
        }
        return bytes;
    }

    @Override public void onDestroy() {
        running = false;
        talking = false;
        if (session != null) {
            session.setActive(false);
            session.release();
            session = null;
        }
        super.onDestroy();
    }

    @Override public IBinder onBind(Intent intent) { return null; }
}
