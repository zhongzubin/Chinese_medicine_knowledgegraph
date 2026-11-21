import { createPinia } from "pinia";
import { createApp } from "vue";
import {
  ElAlert,
  ElButton,
  ElConfigProvider,
  ElInput,
  ElTabPane,
  ElTabs,
  ElTag,
} from "element-plus";
import "element-plus/es/components/base/style/css";
import "element-plus/es/components/alert/style/css";
import "element-plus/es/components/button/style/css";
import "element-plus/es/components/input/style/css";
import "element-plus/es/components/tabs/style/css";
import "element-plus/es/components/tag/style/css";

import App from "@/App.vue";
import router from "@/app/router";
import "@/styles/theme.css";

const app = createApp(App);
app.use(createPinia());
app.use(router);
app.component("ElAlert", ElAlert);
app.component("ElButton", ElButton);
app.component("ElConfigProvider", ElConfigProvider);
app.component("ElInput", ElInput);
app.component("ElTabPane", ElTabPane);
app.component("ElTabs", ElTabs);
app.component("ElTag", ElTag);
app.mount("#app");
