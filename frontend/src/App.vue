<script setup lang="ts">
import zhCn from "element-plus/es/locale/lang/zh-cn";
import { RouterLink, RouterView } from "vue-router";
</script>

<template>
  <el-config-provider :locale="zhCn">
    <div class="app-shell">
      <header class="topbar">
        <div class="topbar__inner">
          <RouterLink class="brand" to="/qa" aria-label="返回证据问答首页">
            <span class="brand__seal" aria-hidden="true">岐</span>
            <span class="brand__copy">
              <strong>岐黄证据图谱</strong>
              <small>Evidence-backed TCM Workbench</small>
            </span>
          </RouterLink>

          <nav class="nav" aria-label="主导航">
            <RouterLink class="nav__link nav__link--active" to="/qa">
              证据问答
            </RouterLink>
            <span class="nav__link nav__link--disabled">
              图谱探索
              <em>下一步</em>
            </span>
            <span class="nav__link nav__link--disabled">
              实体对比
              <em>V1.1</em>
            </span>
            <span class="nav__link nav__link--disabled">
              开发者
              <em>V1.1</em>
            </span>
          </nav>
        </div>
      </header>

      <main class="app-main">
        <RouterView />
      </main>

      <footer class="app-footer">
        <span>内容仅供中医药知识参考，不能替代专业医疗诊断和治疗。</span>
        <span>回答事实来自本轮图谱证据，来源缺失会明确标识。</span>
      </footer>
    </div>
  </el-config-provider>
</template>
