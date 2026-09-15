package dev.radar.jobpilot;

import android.app.Activity;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.view.View;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.webkit.WebChromeClient;
import android.webkit.DownloadListener;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.app.DownloadManager;
import android.content.Context;
import android.net.Uri;
import android.content.Intent;

public class MainActivity extends Activity {
    private WebView web;
    private LinearLayout serverBar;
    private EditText serverUrl;
    private SharedPreferences prefs;
    private static final String KEY_PREF = "server_url";

    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        setContentView(R.layout.activity_main);
        prefs = getSharedPreferences("jobpilot", MODE_PRIVATE);

        web = findViewById(R.id.web);
        serverBar = findViewById(R.id.server_bar);
        serverUrl = findViewById(R.id.server_url);
        Button connect = findViewById(R.id.connect_btn);

        WebSettings st = web.getSettings();
        st.setJavaScriptEnabled(true);
        st.setDomStorageEnabled(true);
        st.setAllowFileAccess(true);
        st.setBuiltInZoomControls(true);
        st.setDisplayZoomControls(false);
        st.setUseWideViewPort(true);
        st.setLoadWithOverviewMode(true);
        st.setMediaPlaybackRequiresUserGesture(false);
        web.setWebViewClient(new WebViewClient());
        web.setWebChromeClient(new WebChromeClient());
        web.setDownloadListener(new DownloadListener() {
            @Override public void onDownloadStart(String url, String userAgent,
                    String contentDisposition, String mimetype, long contentLength) {
                try {
                    String name = android.webkit.URLUtil.guessFileName(url, contentDisposition, mimetype);
                    DownloadManager.Request req = new DownloadManager.Request(Uri.parse(url));
                    req.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                    req.setTitle(name);
                    req.setDestinationInExternalPublicDir(android.os.Environment.DIRECTORY_DOWNLOADS, name);
                    ((DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE)).enqueue(req);
                } catch (Exception e) { /* best-effort */ }
            }
        });

        String saved = prefs.getString(KEY_PREF, "");
        serverUrl.setText(saved);
        if (!saved.isEmpty()) {
            connectServer(saved);
        }

        connect.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                String url = serverUrl.getText().toString().trim();
                connectServer(url);
            }
        });
    }

    private void connectServer(String url) {
        if (url.isEmpty()) return;
        if (!url.startsWith("http")) url = "http://" + url;
        prefs.edit().putString(KEY_PREF, url).apply();
        serverBar.setVisibility(View.GONE);
        web.loadUrl(url);
    }

    @Override public void onBackPressed() {
        if (web != null && web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }
}
