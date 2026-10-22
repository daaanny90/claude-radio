package it.retrobottega.rogerptt;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** After a reboot it tries to start itself; if Android forbids it, just open the app. */
public class BootReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context context, Intent intent) {
        if (!Intent.ACTION_BOOT_COMPLETED.equals(intent.getAction())) return;
        try {
            context.startForegroundService(new Intent(context, PttService.class).setAction(PttService.ACTION_START));
        } catch (Exception ignored) {
        }
    }
}
