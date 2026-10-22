package it.retrobottega.rogerptt;

import android.Manifest;
import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.Settings;
import android.widget.Button;
import android.widget.TextView;

/**
 * A single screen: status (service, microphone, battery, notifications), start and stop, a
 * microphone test, and the buttons that lead to the right settings when something is off.
 */
public class MainActivity extends Activity {
    private TextView status;
    private Button battery;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Runnable tick = new Runnable() {
        @Override public void run() {
            refresh();
            handler.postDelayed(this, 1000);
        }
    };

    @Override protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.main);
        status = findViewById(R.id.status);
        battery = findViewById(R.id.battery);
        Button start = findViewById(R.id.start);
        Button stop = findViewById(R.id.stop);
        Button toggle = findViewById(R.id.toggle);
        Button appInfo = findViewById(R.id.appinfo);
        Button sleeping = findViewById(R.id.sleeping);

        start.setOnClickListener(v -> {
            if (Build.VERSION.SDK_INT >= 33 && !notificationsAllowed()) {
                requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
            }
            startForegroundService(new Intent(this, PttService.class).setAction(PttService.ACTION_START));
            if (!batteryExcluded()) askBatteryExclusion();
            handler.postDelayed(this::refresh, 500);
        });
        stop.setOnClickListener(v -> {
            startService(new Intent(this, PttService.class).setAction(PttService.ACTION_STOP));
            handler.postDelayed(this::refresh, 500);
        });
        toggle.setOnClickListener(v ->
                startService(new Intent(this, PttService.class).setAction(PttService.ACTION_TOGGLE)));
        // Battery optimization exclusion: the system dialog if it is missing, otherwise the list
        battery.setOnClickListener(v -> {
            if (!batteryExcluded()) askBatteryExclusion();
            else open(new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS));
        });
        // App info: on Samsung this is where Battery must be set to "Unrestricted"
        appInfo.setOnClickListener(v -> open(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                Uri.parse("package:" + getPackageName()))));
        // Samsung's "Never sleeping apps": the system Battery screen, from there "Background usage limits"
        sleeping.setOnClickListener(v -> {
            if (!open(new Intent("com.samsung.android.sm.ACTION_BATTERY"))) {
                open(new Intent(Intent.ACTION_POWER_USAGE_SUMMARY));
            }
        });
        // For tests from adb: am start -n it.retrobottega.rogerptt/.MainActivity --ez start true
        if (getIntent().getBooleanExtra("start", false)) start.performClick();
    }

    @Override protected void onResume() {
        super.onResume();
        handler.post(tick);
    }

    @Override protected void onPause() {
        super.onPause();
        handler.removeCallbacks(tick);
    }

    private boolean open(Intent intent) {
        try {
            startActivity(intent);
            return true;
        } catch (ActivityNotFoundException e) {
            return false;
        }
    }

    private void askBatteryExclusion() {
        open(new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                Uri.parse("package:" + getPackageName())));
    }

    private boolean batteryExcluded() {
        return getSystemService(PowerManager.class).isIgnoringBatteryOptimizations(getPackageName());
    }

    private boolean notificationsAllowed() {
        return Build.VERSION.SDK_INT < 33
                || checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED;
    }

    private void refresh() {
        boolean mumla = getPackageManager().getLaunchIntentForPackage(PttService.MUMLA_PACKAGE) != null;
        boolean excluded = batteryExcluded();
        StringBuilder b = new StringBuilder();
        b.append(PttService.running ? "● Service running: the headset button opens and closes Mumla's microphone."
                : "○ Service stopped. Press Start, then just leave the phone in your pocket.");
        b.append("\n\n").append(PttService.running ? (PttService.talking ? "🎙 Microphone OPEN" : "Microphone closed")
                : "Microphone: not managed");
        b.append("\n\n").append(excluded ? "✓ Excluded from Android battery optimization"
                : "✗ NOT excluded from battery optimization: on battery the service may disappear. Press the button below");
        b.append("\n").append("Samsung: also check that it is not among the \"sleeping apps\" (button below, Background usage limits).");
        b.append("\n\n").append(notificationsAllowed() ? "✓ Notifications allowed (the ongoing notification is the sign that the service is alive)"
                : "✗ Notifications denied: the service runs but you cannot see it. App info → Notifications");
        if (!mumla) b.append("\n\n✗ Mumla is not installed.");
        b.append("\n\nSounds: two rising notes = open, falling = closed; long press = close. Turn off Mumla's PTT sound.");
        status.setText(b);
        battery.setText(excluded ? "Battery exclusion: system list" : "Exclude from battery optimization");
    }
}
