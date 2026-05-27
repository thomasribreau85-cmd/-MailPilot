import { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'com.mailpilot.app',
  appName: 'MailPilot',
  webDir: 'www',
  server: {
    // L'app charge directement depuis Railway — pas besoin de build front-end
    url: 'https://mailpilot-production-981d.up.railway.app',
    cleartext: false,
    androidScheme: 'https'
  },
  plugins: {
    SplashScreen: {
      launchShowDuration: 1800,
      launchAutoHide: true,
      backgroundColor: '#060b18',
      androidSplashResourceName: 'splash',
      androidScaleType: 'CENTER_CROP',
      showSpinner: false,
      iosSpinnerStyle: 'small',
      spinnerColor: '#4f6ef7'
    },
    StatusBar: {
      style: 'DARK',
      backgroundColor: '#060b18'
    },
    PushNotifications: {
      presentationOptions: ['badge', 'sound', 'alert']
    },
    Keyboard: {
      resize: 'body',
      style: 'DARK',
      resizeOnFullScreen: true
    }
  },
  ios: {
    contentInset: 'always',
    backgroundColor: '#060b18'
  },
  android: {
    backgroundColor: '#060b18',
    allowMixedContent: false
  }
};

export default config;
