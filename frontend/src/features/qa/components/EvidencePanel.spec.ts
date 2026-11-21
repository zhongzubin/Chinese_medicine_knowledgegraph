import { mount } from "@vue/test-utils";
import ElementPlus from "element-plus";
import { describe, expect, it } from "vitest";

import EvidencePanel from "@/features/qa/components/EvidencePanel.vue";

describe("EvidencePanel", () => {
  it("discloses missing sources instead of inventing a citation", () => {
    const wrapper = mount(EvidencePanel, {
      props: {
        evidence: [
          {
            subject: "麻黄",
            subject_label: "Herb",
            relationship: "HAS_EFFECT",
            object: "发汗",
            object_label: "Effect",
            source: null,
          },
        ],
        facts: [],
        aggregates: [],
        paths: [],
      },
      global: {
        plugins: [ElementPlus],
      },
    });

    expect(wrapper.text()).toContain("当前图谱未提供出处");
  });
});
