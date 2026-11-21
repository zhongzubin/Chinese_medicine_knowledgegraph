import { createRouter, createWebHistory } from "vue-router";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: "/",
      redirect: "/qa",
    },
    {
      path: "/qa",
      name: "qa",
      component: () => import("@/features/qa/QAView.vue"),
      meta: {
        title: "证据问答",
      },
    },
    {
      path: "/:pathMatch(.*)*",
      redirect: "/qa",
    },
  ],
});

router.afterEach((to) => {
  document.title = `${String(to.meta.title || "工作台")} · 岐黄证据图谱`;
  window.scrollTo({ top: 0, behavior: "auto" });
});

export default router;
