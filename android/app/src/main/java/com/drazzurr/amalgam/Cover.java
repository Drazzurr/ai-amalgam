package com.drazzurr.amalgam;

import android.content.Context;
import android.graphics.Color;
import android.graphics.PixelFormat;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;

/**
 * Android only lets the Terminal app start its Linux system while the Terminal is on screen
 * for a moment. This loading screen sits on top while that happens, so you stay on
 * AI Amalgam's screen and never see the Terminal. It always disappears by itself; tapping
 * it removes it at once.
 */
final class Cover {
    private Cover() {}

    private static View view;
    private static final Handler main = new Handler(Looper.getMainLooper());
    private static final long MAX_SHOWN = 15000;

    static boolean available(Context c) {
        return Settings.canDrawOverlays(c);
    }

    static boolean showing() {
        return view != null;
    }

    static void show(Context c) {
        if (view != null || !available(c)) return;
        Context app = c.getApplicationContext();
        try {
            View v = build(app);
            WindowManager.LayoutParams lp = new WindowManager.LayoutParams(
                    WindowManager.LayoutParams.MATCH_PARENT, WindowManager.LayoutParams.MATCH_PARENT,
                    WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                    WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                            | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN
                            | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
                    PixelFormat.OPAQUE);
            app.getSystemService(WindowManager.class).addView(v, lp);
            view = v;
            v.setOnClickListener(x -> hide(app));
            main.postDelayed(() -> hide(app), MAX_SHOWN);     // never left behind
        } catch (Exception e) {
            view = null;
        }
    }

    static void hide(Context c) {
        View v = view;
        if (v == null) return;
        view = null;
        try {
            c.getApplicationContext().getSystemService(WindowManager.class).removeView(v);
        } catch (Exception ignored) {
        }
    }

    /** Hide shortly after AI Amalgam is back on screen, once its page has drawn. */
    static void hideSoon(Context c) {
        if (view != null) main.postDelayed(() -> hide(c), 450);
    }

    private static int dp(Context c, float v) {
        return Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, c.getResources().getDisplayMetrics()));
    }

    private static View build(Context c) {
        FrameLayout root = new FrameLayout(c);
        root.setBackgroundColor(Color.rgb(0x1a, 0x1b, 0x1e));
        LinearLayout col = new LinearLayout(c);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setGravity(Gravity.CENTER_HORIZONTAL);

        ImageView icon = new ImageView(c);
        icon.setImageDrawable(c.getDrawable(R.mipmap.ic_launcher));
        col.addView(icon, new LinearLayout.LayoutParams(dp(c, 96), dp(c, 96)));

        TextView title = new TextView(c);
        title.setText("Starting AI Amalgam");
        title.setTextColor(Color.rgb(0xe8, 0xe9, 0xec));
        title.setTextSize(TypedValue.COMPLEX_UNIT_SP, 22);
        title.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams tl = new LinearLayout.LayoutParams(-2, -2);
        tl.topMargin = dp(c, 18);
        col.addView(title, tl);

        TextView sub = new TextView(c);
        sub.setText("Getting Linux ready…");
        sub.setTextColor(Color.rgb(0xa3, 0xa6, 0xad));
        sub.setTextSize(TypedValue.COMPLEX_UNIT_SP, 15);
        LinearLayout.LayoutParams sl = new LinearLayout.LayoutParams(-2, -2);
        sl.topMargin = dp(c, 6);
        col.addView(sub, sl);

        ProgressBar spin = new ProgressBar(c);
        spin.setIndeterminate(true);
        LinearLayout.LayoutParams pl = new LinearLayout.LayoutParams(dp(c, 36), dp(c, 36));
        pl.topMargin = dp(c, 26);
        col.addView(spin, pl);

        root.addView(col, new FrameLayout.LayoutParams(-2, -2, Gravity.CENTER));
        return root;
    }
}
