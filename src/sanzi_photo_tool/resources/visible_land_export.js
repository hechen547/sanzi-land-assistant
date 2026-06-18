(() => {
  const STATUS_LABELS = {"0": "未完成", "1": "进行中", "2": "已完成"};
  const DEFAULT_COLORS = {"0": "#eafe20", "1": "#fd9f20", "2": "#00fece"};

  function findVueRoot() {
    const app = document.querySelector("#app");
    if (app && app.__vue__) return app.__vue__;
    for (const element of document.querySelectorAll("*")) {
      if (element.__vue__) return element.__vue__;
    }
    return null;
  }

  function findVm(vm, predicate, depth = 0) {
    if (!vm || depth > 14) return null;
    try { if (predicate(vm)) return vm; } catch (_) {}
    for (const child of (vm.$children || [])) {
      const found = findVm(child, predicate, depth + 1);
      if (found) return found;
    }
    return null;
  }

  function walk(nodes, callback) {
    if (!Array.isArray(nodes)) return;
    for (const node of nodes) {
      callback(node);
      walk(node && node.children, callback);
    }
  }

  function escapeXml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;")
      .replace(/'/g, "&apos;");
  }

  function cdata(value) {
    return String(value == null ? "" : value).replace(/]]>/g, "]]]]><![CDATA[>");
  }

  function safeName(value) {
    return String(value || "三资图斑").replace(/[\\/:*?"<>|]/g, "_").replace(/\s+/g, "_");
  }

  function stamp() {
    const pad = value => String(value).padStart(2, "0");
    const now = new Date();
    return `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_`
      + `${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  }

  function normalizeGeometry(geometry) {
    if (!geometry) return null;
    if (geometry.type === "Feature" && geometry.geometry) geometry = geometry.geometry;
    if (!["Polygon", "MultiPolygon"].includes(geometry.type) || !geometry.coordinates) return null;
    return geometry;
  }

  function landCode(properties) {
    return properties.landcode || properties.landCode || properties.LANDCODE
      || properties.dkbm || properties.DKBM || "";
  }

  function coordinateText(ring) {
    return ring.map(coordinate => {
      const lon = Number(coordinate[0]).toFixed(9).replace(/0+$/, "").replace(/\.$/, "");
      const lat = Number(coordinate[1]).toFixed(9).replace(/0+$/, "").replace(/\.$/, "");
      const alt = Number(coordinate[2] || 0);
      return `${lon},${lat},${alt}`;
    }).join(" ");
  }

  function polygonKml(rings) {
    if (!rings || !rings.length) return "";
    let xml = `<Polygon><tessellate>1</tessellate><outerBoundaryIs><LinearRing>`
      + `<coordinates>${coordinateText(rings[0])}</coordinates></LinearRing></outerBoundaryIs>`;
    for (let index = 1; index < rings.length; index++) {
      xml += `<innerBoundaryIs><LinearRing><coordinates>${coordinateText(rings[index])}`
        + `</coordinates></LinearRing></innerBoundaryIs>`;
    }
    return xml + "</Polygon>";
  }

  function geometryKml(geometry) {
    if (geometry.type === "Polygon") return polygonKml(geometry.coordinates);
    return `<MultiGeometry>${geometry.coordinates.map(polygonKml).join("")}</MultiGeometry>`;
  }

  function kmlColor(hex, alpha) {
    const match = String(hex || "").match(/^#?([0-9a-fA-F]{6})$/);
    if (!match) return `${alpha}00ffff`;
    const color = match[1].toLowerCase();
    return `${alpha}${color.slice(4, 6)}${color.slice(2, 4)}${color.slice(0, 2)}`;
  }

  function description(feature) {
    const rows = Object.entries(feature.properties)
      .filter(([key]) => !key.startsWith("_"))
      .map(([key, value]) => `<tr><td>${escapeXml(key)}</td><td>${escapeXml(value)}</td></tr>`)
      .join("");
    return `<table border="1" cellpadding="3" cellspacing="0">${rows}</table>`;
  }

  function extendedData(properties) {
    return "<ExtendedData>" + Object.entries(properties)
      .filter(([key]) => !key.startsWith("_"))
      .map(([key, value]) => `<Data name="${escapeXml(key)}"><value>${escapeXml(value)}</value></Data>`)
      .join("") + "</ExtendedData>";
  }

  function buildKml(filename, features, states) {
    const xml = [
      '<?xml version="1.0" encoding="UTF-8"?>',
      '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
      `<name>${escapeXml(filename)}</name>`
    ];
    for (const state of states) {
      xml.push(`<Style id="status${state.value}"><LineStyle><color>${kmlColor(state.color, "ff")}`
        + `</color><width>2.5</width></LineStyle><PolyStyle><color>${kmlColor(state.color, "55")}`
        + `</color><fill>1</fill><outline>1</outline></PolyStyle></Style>`);
      xml.push(`<Folder><name>${escapeXml(state.label)}</name>`);
      for (const feature of features.filter(item => item.statusValue === state.value)) {
        xml.push(`<Placemark><name>${escapeXml(feature.name)}</name>`
          + `<description><![CDATA[${cdata(description(feature))}]]></description>`
          + extendedData(feature.properties)
          + `<styleUrl>#status${state.value}</styleUrl>${geometryKml(feature.geometry)}</Placemark>`);
      }
      xml.push("</Folder>");
    }
    xml.push("</Document></kml>");
    return xml.join("\n");
  }

  const root = findVueRoot();
  if (!root) return {ok: false, message: "没有找到三资平台页面，请先登录并进入数据采集地图。"};
  const mainVm = findVm(root, vm =>
    vm.currentDistrictCode || vm.defaultDistrictCode || vm.cachedDistrictData
    || vm.mapComponent || (vm.$refs && vm.$refs.twoDMap)
  );
  const treeVm = findVm(root, vm =>
    vm.$options && vm.$options.name === "oneMapTree" && Array.isArray(vm.treeData)
  );
  let mapComponent = mainVm && mainVm.$refs && mainVm.$refs.twoDMap;
  if (!mapComponent && mainVm && mainVm.mapComponent && mainVm.mapComponent.map) {
    mapComponent = mainVm.mapComponent;
  }
  if (!mapComponent) {
    mapComponent = findVm(root, vm =>
      vm.$options && vm.$options.name === "LeafletMap" && vm.map
    );
  }
  const map = mapComponent && mapComponent.map;
  if (!map || typeof map.eachLayer !== "function") {
    return {ok: false, message: "没有找到地图。请进入数据采集页面，并等待地图加载完成。"};
  }

  const districtCode = (mainVm && (mainVm.currentDistrictCode || mainVm.defaultDistrictCode))
    || (mainVm && mainVm.cachedDistrictData && mainVm.cachedDistrictData.distinctCode) || "";
  const village = (mainVm && mainVm.cachedDistrictData && mainVm.cachedDistrictData.distinctName)
    || "三资图斑";
  const colors = {};
  if (treeVm) {
    walk(treeVm.treeData, node => {
      if (node && node.filterField === "landstatus" && node.typeValue != null) {
        colors[String(node.typeValue)] = node.color || DEFAULT_COLORS[String(node.typeValue)];
      }
    });
  }

  const features = [];
  map.eachLayer(layer => {
    if (!layer || !layer.feature || !layer.toGeoJSON) return;
    const sourceProperties = layer.feature.properties || {};
    if (sourceProperties.landstatus == null) return;
    const statusValue = String(sourceProperties.landstatus);
    if (!(statusValue in STATUS_LABELS)) return;
    let geometry;
    try { geometry = normalizeGeometry(layer.toGeoJSON().geometry); } catch (_) { return; }
    if (!geometry) return;
    const properties = {};
    for (const [key, value] of Object.entries(sourceProperties)) {
      if (value != null) properties[key] = typeof value === "object" ? JSON.stringify(value) : String(value);
    }
    properties._statusColor = colors[statusValue] || DEFAULT_COLORS[statusValue];
    const code = landCode(properties);
    features.push({
      name: code || `图斑${features.length + 1}`,
      landcode: code,
      statusValue,
      geometry,
      properties
    });
  });

  const seen = new Set();
  const unique = features.filter(feature => {
    const key = feature.landcode || `${feature.statusValue}|${JSON.stringify(feature.geometry.coordinates).slice(0, 220)}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  if (!unique.length) {
    return {ok: false, message: "当前地图上没有已显示的工作进度图斑。请先勾选工作进度并等待图斑出现。"};
  }
  const stateValues = [...new Set(unique.map(feature => feature.statusValue))].sort().reverse();
  const states = stateValues.map(value => ({
    value,
    label: STATUS_LABELS[value],
    color: colors[value] || DEFAULT_COLORS[value]
  }));
  const filename = `${safeName(village)}_已显示工作进度_${states.map(s => s.label).join("_")}_${stamp()}`;
  const blob = new Blob([buildKml(filename, unique, states)], {
    type: "application/vnd.google-earth.kml+xml;charset=utf-8"
  });
  const anchor = document.createElement("a");
  anchor.href = URL.createObjectURL(blob);
  anchor.download = `${filename}.kml`;
  document.body.appendChild(anchor);
  anchor.click();
  setTimeout(() => {
    URL.revokeObjectURL(anchor.href);
    anchor.remove();
  }, 2000);
  return {
    ok: true,
    village,
    districtCode,
    featureCount: unique.length,
    states: states.map(state => state.label),
    filename: `${filename}.kml`
  };
})()
