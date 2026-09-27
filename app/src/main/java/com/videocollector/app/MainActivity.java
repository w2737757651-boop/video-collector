package com.videocollector.app;

import android.Manifest;
import android.app.Activity;
import android.app.DownloadManager;
import android.content.Context;
import android.content.ClipboardManager;
import android.content.ClipData;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

public class MainActivity extends Activity {
    private static final String HOME="https://video-collector-0d1n.onrender.com";
    private static final int REQ_STORAGE=100;

    private WebView webView;
    private LinearLayout downloadPanel;
    private TextView downloadText;
    private ProgressBar downloadProgress;
    private DownloadManager downloadManager;
    private Handler handler;
    private long currentDownloadId=-1L;

    @Override
    protected void onCreate(Bundle savedInstanceState){
        super.onCreate(savedInstanceState);

        downloadManager=(DownloadManager)getSystemService(Context.DOWNLOAD_SERVICE);
        handler=new Handler(Looper.getMainLooper());

        LinearLayout root=new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(0xFF101317);

        webView=new WebView(this);
        root.addView(webView,new LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,0,1f));

        downloadPanel=new LinearLayout(this);
        downloadPanel.setOrientation(LinearLayout.VERTICAL);
        downloadPanel.setPadding(24,14,24,18);
        downloadPanel.setBackgroundColor(0xFF181D23);
        downloadPanel.setVisibility(View.GONE);

        downloadText=new TextView(this);
        downloadText.setTextColor(0xFFF4F7FA);
        downloadText.setTextSize(14f);

        downloadProgress=new ProgressBar(this,null,android.R.attr.progressBarStyleHorizontal);
        downloadProgress.setMax(100);

        downloadPanel.addView(downloadText);
        LinearLayout.LayoutParams pp=new LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,22);
        pp.topMargin=10;
        downloadPanel.addView(downloadProgress,pp);
        root.addView(downloadPanel);

        setContentView(root);

        WebSettings s=webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(true);
        s.setUserAgentString(s.getUserAgentString()+" VideoCollectorApp/2.0");

        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView,true);

        webView.addJavascriptInterface(new AndroidDownloader(),"AndroidDownloader");
        webView.addJavascriptInterface(new AndroidClipboard(),"AndroidClipboard");
        webView.setWebViewClient(new WebViewClient());
        webView.setWebChromeClient(new WebChromeClient());
        webView.loadUrl(HOME);
    }

    public class AndroidDownloader{
        @JavascriptInterface
        public void download(final String url,final String filename){
            runOnUiThread(() -> startDownload(url,filename));
        }
    }


    public class AndroidClipboard{
        @JavascriptInterface
        public String getText(){
            try{
                ClipboardManager cm=(ClipboardManager)getSystemService(Context.CLIPBOARD_SERVICE);
                if(cm==null || !cm.hasPrimaryClip()) return "";

                ClipData clip=cm.getPrimaryClip();
                if(clip==null || clip.getItemCount()==0) return "";

                CharSequence text=clip.getItemAt(0).coerceToText(MainActivity.this);
                return text==null ? "" : text.toString();
            }catch(Exception e){
                return "";
            }
        }
    }


    private void startDownload(String url,String filename){
        if(Build.VERSION.SDK_INT<=Build.VERSION_CODES.P &&
           checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE)!=PackageManager.PERMISSION_GRANTED){
            requestPermissions(new String[]{Manifest.permission.WRITE_EXTERNAL_STORAGE},REQ_STORAGE);
            Toast.makeText(this,"请允许存储权限后重新点击下载",Toast.LENGTH_LONG).show();
            return;
        }

        try{
            filename=sanitize(filename);

            DownloadManager.Request req=new DownloadManager.Request(Uri.parse(url));
            req.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            String cookie=CookieManager.getInstance().getCookie(url);
            if(cookie!=null) req.addRequestHeader("Cookie",cookie);
            req.addRequestHeader("User-Agent",webView.getSettings().getUserAgentString());
            req.setTitle(filename);
            req.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS,filename);

            currentDownloadId=downloadManager.enqueue(req);
            downloadPanel.setVisibility(View.VISIBLE);
            downloadProgress.setIndeterminate(false);
            downloadProgress.setProgress(0);
            downloadText.setText("正在下载："+filename+"  0%");
            poll(currentDownloadId,filename);
        }catch(Exception e){
            Toast.makeText(this,"下载启动失败："+e.getMessage(),Toast.LENGTH_LONG).show();
        }
    }

    private String sanitize(String s){
        if(s==null||s.trim().isEmpty()) return "download";
        return s.replaceAll("[\\\\/:*?\"<>|]","_");
    }

    private void poll(final long id,final String filename){
        handler.postDelayed(() -> {
            if(id!=currentDownloadId) return;

            DownloadManager.Query q=new DownloadManager.Query().setFilterById(id);
            Cursor c=null;
            try{
                c=downloadManager.query(q);
                if(c==null||!c.moveToFirst()){
                    poll(id,filename);
                    return;
                }

                int status=c.getInt(c.getColumnIndexOrThrow(DownloadManager.COLUMN_STATUS));
                long done=c.getLong(c.getColumnIndexOrThrow(DownloadManager.COLUMN_BYTES_DOWNLOADED_SO_FAR));
                long total=c.getLong(c.getColumnIndexOrThrow(DownloadManager.COLUMN_TOTAL_SIZE_BYTES));

                if(status==DownloadManager.STATUS_SUCCESSFUL){
                    downloadProgress.setIndeterminate(false);
                    downloadProgress.setProgress(100);
                    downloadText.setText("下载完成："+filename+"  100%");
                    Toast.makeText(this,"下载完成，已保存到 Download 文件夹",Toast.LENGTH_LONG).show();
                    currentDownloadId=-1L;
                    return;
                }

                if(status==DownloadManager.STATUS_FAILED){
                    downloadText.setText("下载失败："+filename);
                    currentDownloadId=-1L;
                    return;
                }

                if(total>0){
                    int percent=(int)(done*100L/total);
                    downloadProgress.setIndeterminate(false);
                    downloadProgress.setProgress(percent);
                    downloadText.setText("正在下载："+filename+"  "+percent+"%");
                }else{
                    downloadProgress.setIndeterminate(true);
                    double mb=done/1024.0/1024.0;
                    downloadText.setText(String.format("正在下载：%s  %.1f MB",filename,mb));
                }

                poll(id,filename);
            }catch(Exception e){
                poll(id,filename);
            }finally{
                if(c!=null)c.close();
            }
        },500);
    }

    @Override
    public void onBackPressed(){
        if(webView!=null&&webView.canGoBack()) webView.goBack();
        else super.onBackPressed();
    }
}
