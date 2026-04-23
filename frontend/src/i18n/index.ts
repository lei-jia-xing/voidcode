import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import enLocales from "./locales/en.json";
import zhCNLocales from "./locales/zh-CN.json";

const resources = {
  en: {
    translation: enLocales,
  },
  "zh-CN": {
    translation: zhCNLocales,
  },
};

i18n.use(initReactI18next).init({
  resources,
  lng: "en", // default language, overridden by store
  fallbackLng: "en",
  interpolation: {
    escapeValue: false,
  },
});

export default i18n;
