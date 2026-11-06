/* NYC charter school campus map.
   Data: data/campuses.geojson and data/zip_centroids.json, built by scripts/build_data.py */
(function () {
  "use strict";

  var GRADE_ORDER = ["PK", "K", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"];
  var BOROUGHS = ["Bronx", "Brooklyn", "Manhattan", "Queens", "Staten Island"];
  var LEVEL_FILTERS = [
    { key: "pk", label: "Pre-K", grades: ["PK"] },
    { key: "elem", label: "Elementary (K-5)", grades: ["K", "1", "2", "3", "4", "5"] },
    { key: "mid", label: "Middle (6-8)", grades: ["6", "7", "8"] },
    { key: "high", label: "High (9-12)", grades: ["9", "10", "11", "12"] }
  ];
  var PIN_LEVELS = {
    elem: "Elementary", elemmid: "Elementary + Middle", mid: "Middle", high: "High (incl. 6-12)", k12: "K-12"
  };
  var GEOSEARCH = "https://geosearch.planninglabs.nyc/v2/search";

  // ---------------------------------------------------------------- helpers
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function norm(s) {
    return String(s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "")
      .replace(/[–—]/g, "-").replace(/[^a-z0-9]+/g, " ").trim();
  }
  function gradeList(span) {
    if (!span) return [];
    var parts = span.split("-"), a = GRADE_ORDER.indexOf(parts[0]), b = GRADE_ORDER.indexOf(parts[1] || parts[0]);
    return a < 0 || b < 0 ? [] : GRADE_ORDER.slice(a, b + 1);
  }
  function pinLevel(grades) {
    var has = function (list) { return list.some(function (g) { return grades.indexOf(g) >= 0; }); };
    var e = has(["PK", "K", "1", "2", "3", "4", "5"]), m = has(["6", "7", "8"]), h = has(["9", "10", "11", "12"]);
    if (e && h) return "k12";
    if (h) return "high";
    if (e && m) return "elemmid";
    if (m) return "mid";
    return "elem";
  }
  function miles(lat1, lon1, lat2, lon2) {
    var r = Math.PI / 180, dLat = (lat2 - lat1) * r, dLon = (lon2 - lon1) * r;
    var x = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
      Math.cos(lat1 * r) * Math.cos(lat2 * r) * Math.sin(dLon / 2) * Math.sin(dLon / 2);
    return 3958.8 * 2 * Math.atan2(Math.sqrt(x), Math.sqrt(1 - x));
  }
  function debounce(fn, ms) {
    var t; return function () { clearTimeout(t); t = setTimeout(fn, ms); };
  }
  var $ = function (id) { return document.getElementById(id); };

  // ---------------------------------------------------------------- map
  var map = L.map("map", { zoomControl: false });
  L.control.zoom({ position: "topright" }).addTo(map);
  var isPhone = function () { return window.matchMedia("(max-width: 760px)").matches; };
  var panel = $("panel");
  // Keep fitted views and popups clear of the floating panel (desktop) or the bottom sheet (phone).
  function viewPadding() {
    if (isPhone()) return { tl: [20, 20], br: [20, panel.getBoundingClientRect().height + 20] };
    return { tl: [panel.hidden ? 20 : panel.getBoundingClientRect().right + 20, 20], br: [70, 20] };
  }
  // OpenStreetMap tiles (free with attribution), softened in CSS so the coloured pins stand out.
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, className: "basemap",
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  }).addTo(map);
  map.setView([40.70, -73.94], 11);

  var cluster = L.markerClusterGroup({
    maxClusterRadius: 32, disableClusteringAtZoom: 15, spiderfyOnMaxZoom: true, showCoverageOnHover: false,
    iconCreateFunction: function (c) {
      var n = 0;
      c.getAllChildMarkers().forEach(function (m) { n += m.options.campuses; });
      var size = n < 10 ? 30 : n < 30 ? 36 : 42;
      return L.divIcon({ className: "", iconSize: [size, size],
        html: '<div class="cluster" style="width:' + size + "px;height:" + size + 'px">' + n + "</div>" });
    }
  }).addTo(map);
  var hereLayer = L.layerGroup().addTo(map);

  // ---------------------------------------------------------------- state
  var campuses = [], zips = {}, totalSchools = 0;
  var state = { q: "", borough: "", levels: [], authorizer: "", district: "", newOnly: false, near: null };
  var markerByCampus = {};

  // ---------------------------------------------------------------- popups
  function campusHtml(p, showAddress) {
    var note = p.grades_2025_26
      ? ' <span class="note">(as of 2025-26; school is ' + esc(p.school_grades) + " in 2026-27)</span>"
      : (p.school_campuses > 1 && p.school_grades && p.school_grades !== p.grades
        ? ' <span class="note">(whole school: ' + esc(p.school_grades) + ")</span>" : "");
    var links = [];
    if (p.phone) links.push('<a href="tel:' + esc(p.phone.replace(/[^\d+]/g, "")) + '">' + esc(p.phone) + "</a>");
    if (p.website) links.push('<a href="' + esc(p.website) + '" target="_blank" rel="noopener">Website</a>');
    links.push('<a href="https://www.google.com/maps/dir/?api=1&destination=' +
      encodeURIComponent(p.address) + '" target="_blank" rel="noopener">Directions</a>');
    return '<div class="school">' +
      "<h2>" + esc(p.school) + (p.new ? ' <span class="badge">New in 2026-27</span>' : "") + "</h2>" +
      (p.campus ? '<p class="campus">' + esc(p.campus) + "</p>" : "") +
      (p.also_known_as ? '<p class="campus">State name: ' + esc(p.also_known_as) + "</p>" : "") +
      (showAddress ? '<p class="address">' + esc(p.address) + "</p>" : "") +
      "<dl>" +
      "<dt>Grades</dt><dd>" + esc(p.grades) + note + "</dd>" +
      "<dt>District</dt><dd>" + esc(p.district) + " · " + esc(p.borough) + "</dd>" +
      "<dt>Authorizer</dt><dd>" + esc(p.authorizer) + "</dd>" +
      "<dt>Opened</dt><dd>" + esc(p.opened) + "</dd>" +
      (p.school_campuses > 1 ? "<dt>Campuses</dt><dd>" + p.school_campuses + " (this is one)</dd>" : "") +
      "</dl>" +
      (p.approximate ? '<p class="note">Approximate location: this address is not in the city address database, so the pin is at the nearest address on the same street.</p>' : "") +
      '<div class="links">' + links.join("") + "</div></div>";
  }
  function buildingHtml(shown, hidden) {
    var shared = shown.length + hidden > 1;
    var head = shared
      ? '<p class="building">' + (shown.length + hidden) + " schools share this building</p>" +
        '<p class="address">' + esc(shown[0].address) + "</p>" +
        (hidden ? '<p class="note">' + hidden + " of them " + (hidden === 1 ? "doesn't" : "don't") + " match your filters.</p>" : "")
      : "";
    return '<div class="popup">' + head + shown.map(function (p) { return campusHtml(p, !shared); }).join("") + "</div>";
  }
  function iconFor(shown) {
    if (shown.length > 1) {
      return L.divIcon({ className: "", iconSize: [22, 22], iconAnchor: [11, 11], popupAnchor: [0, -10],
        html: '<div class="pin shared lvl-shared">' + shown.length + "</div>" });
    }
    return L.divIcon({ className: "", iconSize: [14, 14], iconAnchor: [7, 7], popupAnchor: [0, -6],
      html: '<div class="pin lvl-' + shown[0].pin + '"></div>' });
  }

  // ---------------------------------------------------------------- filtering
  function matches(p) {
    if (state.borough && p.borough !== state.borough) return false;
    if (state.authorizer && p.authorizer !== state.authorizer) return false;
    if (state.district && p.district !== state.district) return false;
    if (state.newOnly && !p.new) return false;
    if (state.levels.length) {
      var ok = state.levels.some(function (k) {
        var lf = LEVEL_FILTERS.filter(function (x) { return x.key === k; })[0];
        return lf.grades.some(function (g) { return p.gradeList.indexOf(g) >= 0; });
      });
      if (!ok) return false;
    }
    if (state.q) {
      // every typed word must start a word in the school's name, campus name or address ("4" won't match "10457")
      var words = norm(state.q).split(" ");
      for (var i = 0; i < words.length; i++) if (p.haystack.indexOf(" " + words[i]) < 0) return false;
    }
    return true;
  }

  function activeFilterCount() {
    return (state.borough ? 1 : 0) + (state.levels.length ? 1 : 0) + (state.authorizer ? 1 : 0) +
      (state.district ? 1 : 0) + (state.newOnly ? 1 : 0);
  }

  // ---------------------------------------------------------------- render
  function render(opts) {
    opts = opts || {};
    var shown = campuses.filter(matches);
    var shownIds = {};
    shown.forEach(function (p) { shownIds[p.id] = true; });

    // markers: one per building, listing every matching school in it
    cluster.clearLayers();
    markerByCampus = {};
    var buildings = {}, order = [];
    shown.forEach(function (p) {
      if (!buildings[p.building]) { buildings[p.building] = []; order.push(p.building); }
      buildings[p.building].push(p);
    });
    var markers = order.map(function (key) {
      var group = buildings[key].sort(function (a, b) { return a.school.localeCompare(b.school); });
      var hidden = campuses.filter(function (c) { return c.building === key && !shownIds[c.id]; }).length;
      var title = group.length > 1
        ? group.length + " schools: " + group.map(function (p) { return p.school; }).join("; ")
        : group[0].school;
      var m = L.marker([group[0].lat, group[0].lon], {
        icon: iconFor(group), title: title, alt: title, riseOnHover: true, campuses: group.length
      }).bindPopup(buildingHtml(group, hidden), {
        maxWidth: 320, autoPanPaddingTopLeft: [16, 16],
        autoPanPaddingBottomRight: isPhone() ? [16, panel.getBoundingClientRect().height + 16] : [64, 16]
      });
      group.forEach(function (p) { markerByCampus[p.id] = m; });
      return m;
    });
    cluster.addLayers(markers);

    // list: one entry per school, with each matching campus
    var bySchool = {}, schools = [];
    shown.forEach(function (p) {
      if (!bySchool[p.doe_code]) { bySchool[p.doe_code] = { name: p.school, first: p, campuses: [] }; schools.push(bySchool[p.doe_code]); }
      bySchool[p.doe_code].campuses.push(p);
    });
    if (state.near) {
      schools.forEach(function (s) {
        s.campuses.forEach(function (p) { p.dist = miles(state.near.lat, state.near.lon, p.lat, p.lon); });
        s.campuses.sort(function (a, b) { return a.dist - b.dist; });
        s.dist = s.campuses[0].dist;
      });
      schools.sort(function (a, b) { return a.dist - b.dist; });
    } else {
      schools.sort(function (a, b) { return a.name.localeCompare(b.name); });
    }

    var list = $("list");
    if (!schools.length) {
      list.innerHTML = '<li class="empty">No schools match. Try removing a filter or changing the search.</li>';
    } else {
      list.innerHTML = schools.map(function (s) {
        var p = s.first;
        var meta = [p.borough, "Grades " + p.school_grades, p.authorizer].join(" · ");
        var camp = s.campuses.map(function (c) {
          // show the campus's own name when it has one (e.g. a charter's high school under a different name)
          var label = (c.campus ? esc(c.campus) + "<br><small>" + esc(c.address) + "</small>" : esc(c.address)) +
            (p.school_campuses > 1 && c.grades ? " <small>(grades " + esc(c.grades) + ")</small>" : "");
          return '<li><button type="button" class="campus-btn" data-id="' + c.id + '">' +
            '<span class="pin lvl-' + c.pin + '" aria-hidden="true"></span><span>' + label +
            (state.near && s.campuses.length > 1 ? " <small>" + c.dist.toFixed(1) + " mi</small>" : "") + "</span>" +
            '<svg class="go" viewBox="0 0 20 20" aria-hidden="true"><path d="M8 5l5 5-5 5"/></svg></button></li>';
        }).join("");
        return "<li><h2><span>" + esc(s.name) + (p.new ? ' <span class="badge">New</span>' : "") + "</span>" +
          (state.near ? '<span class="dist">' + s.dist.toFixed(1) + " mi</span>" : "") + "</h2>" +
          '<p class="meta">' + esc(meta) + (p.school_campuses > s.campuses.length
            ? " · " + s.campuses.length + " of " + p.school_campuses + " campuses match" : "") + "</p>" +
          '<ul class="campuses">' + camp + "</ul></li>";
      }).join("");
    }

    var filtered = shown.length !== campuses.length;
    $("summary").innerHTML = "<strong>" + schools.length + (schools.length === 1 ? " school" : " schools") + "</strong> at " +
      shown.length + (shown.length === 1 ? " campus" : " campuses") +
      (filtered ? " (of " + totalSchools + " schools)" : "") +
      (state.near ? " · nearest first" : "");
    var n = activeFilterCount();
    $("filter-count").hidden = !n;
    $("filter-count").textContent = n;

    if (opts.fit && markers.length && !state.near) {
      var pad = viewPadding();
      map.fitBounds(L.featureGroup(markers).getBounds(), { paddingTopLeft: pad.tl, paddingBottomRight: pad.br, maxZoom: 15 });
    }
  }

  function showCampus(id) {
    var m = markerByCampus[id];
    if (!m) return;
    if (isPhone()) setSheet(false);
    cluster.zoomToShowLayer(m, function () { m.openPopup(); });
  }

  function setSheet(open) {
    panel.classList.toggle("sheet-open", open);
    if (!open) panel.querySelector(".panel-scroll").scrollTop = 0;
    $("sheet-handle").setAttribute("aria-expanded", open ? "true" : "false");
    $("sheet-handle").setAttribute("aria-label", open ? "Show more of the map" : "Expand the school list");
  }

  // ---------------------------------------------------------------- near me
  function setNear(lat, lon, label) {
    state.near = { lat: lat, lon: lon, label: label };
    hereLayer.clearLayers();
    L.marker([lat, lon], {
      icon: L.divIcon({ className: "", iconSize: [18, 18], iconAnchor: [9, 9], html: '<div class="here-pin"></div>' }),
      title: "You searched: " + label, zIndexOffset: 1000, keyboard: false
    }).addTo(hereLayer);
    var pad = viewPadding();
    map.fitBounds(L.latLng(lat, lon).toBounds(1600), { paddingTopLeft: pad.tl, paddingBottomRight: pad.br });
    $("near-status").innerHTML = "Sorted by distance from <strong>" + esc(label) + "</strong>. " +
      '<button type="button" class="btn-text" id="near-clear">Clear</button>';
    $("near-clear").addEventListener("click", clearNear);
    render();
  }
  function clearNear() {
    state.near = null;
    hereLayer.clearLayers();
    $("near").value = "";
    $("near-status").textContent = "";
    render({ fit: true });
  }
  function findNear(text) {
    text = text.trim();
    if (!text) return;
    if (/^\d{5}$/.test(text)) {
      if (zips[text]) setNear(zips[text][0], zips[text][1], "ZIP " + text);
      else $("near-status").textContent = "ZIP " + text + " isn't a New York City ZIP code. Try a street address instead.";
      return;
    }
    $("near-status").textContent = "Searching…";
    fetch(GEOSEARCH + "?size=1&text=" + encodeURIComponent(text))
      .then(function (r) { return r.json(); })
      .then(function (j) {
        var f = j.features && j.features[0];
        if (!f) throw new Error("not found");
        setNear(f.geometry.coordinates[1], f.geometry.coordinates[0], f.properties.label.replace(/, USA$/, ""));
      })
      .catch(function () {
        $("near-status").textContent = "We couldn't find that address. Try a street address with the borough, like \"350 5th Ave, Manhattan\", or a 5-digit ZIP code.";
      });
  }

  // ---------------------------------------------------------------- legend
  function addLegend() {
    var ctl = L.control({ position: "bottomright" });
    ctl.onAdd = function () {
      var div = L.DomUtil.create("div", "legend glass");
      var rows = Object.keys(PIN_LEVELS).map(function (k) {
        return '<div class="row"><span class="pin lvl-' + k + '"></span>' + PIN_LEVELS[k] + "</div>";
      }).join("") +
        '<div class="row"><span class="pin shared lvl-shared">3</span>Shared building (number of schools)</div>' +
        '<div class="row"><span class="cluster legend-cluster">12</span>Nearby campuses (zoom in)</div>' +
        '<div class="row"><span class="here-pin" style="width:14px;height:14px;border-width:2px"></span>Your search</div>';
      div.innerHTML = '<button type="button" class="legend-toggle" aria-expanded="false">Legend</button><div class="rows">' + rows + "</div>";
      var btn = div.querySelector(".legend-toggle");
      btn.addEventListener("click", function () {
        var open = div.classList.toggle("open");
        btn.setAttribute("aria-expanded", open ? "true" : "false");
      });
      L.DomEvent.disableClickPropagation(div);
      return div;
    };
    ctl.addTo(map);
  }

  // ---------------------------------------------------------------- controls
  function buildControls() {
    var b = $("borough");
    b.insertAdjacentHTML("beforeend", [""].concat(BOROUGHS).map(function (name) {
      return '<label><input type="radio" name="borough" value="' + esc(name) + '"' + (name ? "" : " checked") + "><span>" +
        (name || "All") + "</span></label>";
    }).join(""));
    $("levels").insertAdjacentHTML("beforeend", LEVEL_FILTERS.map(function (l) {
      return '<label><input type="checkbox" name="level" value="' + l.key + '"><span>' + l.label + "</span></label>";
    }).join(""));

    var auths = {}, dists = {};
    campuses.forEach(function (p) { auths[p.authorizer] = 1; dists[p.district] = 1; });
    Object.keys(auths).sort().forEach(function (a) { $("authorizer").insertAdjacentHTML("beforeend", "<option>" + esc(a) + "</option>"); });
    Object.keys(dists).sort(function (a, c) { return a - c; }).forEach(function (d) {
      $("district").insertAdjacentHTML("beforeend", '<option value="' + esc(d) + '">District ' + esc(d) + "</option>");
    });

    if (!isPhone()) $("filters").open = true;

    // appearance: Auto (follow the device) -> Light -> Dark, remembered on this device
    var THEMES = {
      auto:  { next: "light", label: "automatic", icon: '<circle cx="10" cy="10" r="6.5"/><path d="M10 3.5v13a6.5 6.5 0 0 0 0-13z" fill="currentColor"/>' },
      light: { next: "dark", label: "light", icon: '<circle cx="10" cy="10" r="3.5"/><path d="M10 2v2M10 16v2M2 10h2M16 10h2M4.3 4.3l1.4 1.4M14.3 14.3l1.4 1.4M4.3 15.7l1.4-1.4M14.3 5.7l1.4-1.4"/>' },
      dark:  { next: "auto", label: "dark", icon: '<path d="M16 12.5A6.5 6.5 0 0 1 7.5 4a6.5 6.5 0 1 0 8.5 8.5z"/>' }
    };
    function applyTheme(t) {
      if (t === "auto") document.documentElement.removeAttribute("data-theme");
      else document.documentElement.setAttribute("data-theme", t);
      $("theme").innerHTML = '<svg viewBox="0 0 20 20" aria-hidden="true">' + THEMES[t].icon + "</svg>";
      $("theme").setAttribute("aria-label", "Appearance: " + THEMES[t].label + ". Switch to " + THEMES[THEMES[t].next].label);
      $("theme").title = "Appearance: " + THEMES[t].label;
    }
    var theme = "auto";
    try { theme = localStorage.getItem("theme") || "auto"; } catch (e) { /* storage unavailable */ }
    if (!THEMES[theme]) theme = "auto";
    applyTheme(theme);
    $("theme").addEventListener("click", function () {
      theme = THEMES[theme].next;
      applyTheme(theme);
      try { localStorage.setItem("theme", theme); } catch (e) { /* storage unavailable */ }
    });

    // desktop: hide/show the panel (remembered on this device)
    function setPanel(open, focus) {
      panel.hidden = !open;
      $("panel-open").hidden = open;
      $("panel-open").setAttribute("aria-expanded", open ? "true" : "false");
      try { localStorage.setItem("panelHidden", open ? "0" : "1"); } catch (e) { /* storage unavailable */ }
      if (focus) (open ? $("q") : $("panel-open")).focus();
    }
    $("panel-close").addEventListener("click", function () { setPanel(false, true); });
    $("panel-open").addEventListener("click", function () { setPanel(true, true); });
    try { if (!isPhone() && localStorage.getItem("panelHidden") === "1") setPanel(false, false); } catch (e) { /* ignore */ }

    // phone: bottom sheet expands for searching and browsing, shrinks to show the map
    $("sheet-handle").addEventListener("click", function () { setSheet(!panel.classList.contains("sheet-open")); });
    ["q", "near"].forEach(function (id) {
      $(id).addEventListener("focus", function () { if (isPhone()) setSheet(true); });
    });
    $("filters").addEventListener("toggle", function () { if (isPhone() && $("filters").open) setSheet(true); });

    $("q").addEventListener("input", debounce(function () { state.q = $("q").value; render({ fit: true }); }, 200));
    $("search-form").addEventListener("submit", function (e) { e.preventDefault(); });
    b.addEventListener("change", function (e) { state.borough = e.target.value; render({ fit: true }); });
    $("levels").addEventListener("change", function () {
      state.levels = Array.prototype.map.call(document.querySelectorAll('input[name="level"]:checked'), function (x) { return x.value; });
      render({ fit: true });
    });
    $("authorizer").addEventListener("change", function () { state.authorizer = $("authorizer").value; render({ fit: true }); });
    $("district").addEventListener("change", function () { state.district = $("district").value; render({ fit: true }); });
    $("new-only").addEventListener("change", function () { state.newOnly = $("new-only").checked; render({ fit: true }); });
    $("reset").addEventListener("click", function () {
      state.q = ""; state.borough = ""; state.levels = []; state.authorizer = ""; state.district = ""; state.newOnly = false;
      $("q").value = ""; $("authorizer").value = ""; $("district").value = ""; $("new-only").checked = false;
      document.querySelector('input[name="borough"][value=""]').checked = true;
      document.querySelectorAll('input[name="level"]').forEach(function (x) { x.checked = false; });
      render({ fit: true });
    });
    $("near-form").addEventListener("submit", function (e) { e.preventDefault(); findNear($("near").value); });
    $("list").addEventListener("click", function (e) {
      var btn = e.target.closest(".campus-btn");
      if (btn) showCampus(btn.getAttribute("data-id"));
    });
  }

  // ---------------------------------------------------------------- load
  Promise.all([
    fetch("data/campuses.geojson").then(function (r) { return r.json(); }),
    fetch("data/zip_centroids.json").then(function (r) { return r.json(); }).catch(function () { return {}; })
  ]).then(function (res) {
    zips = res[1];
    var codes = {};
    campuses = res[0].features.map(function (f, i) {
      var p = f.properties;
      p.id = String(i);
      p.lat = f.geometry.coordinates[1];
      p.lon = f.geometry.coordinates[0];
      p.building = p.bin || (p.lat.toFixed(5) + "," + p.lon.toFixed(5));
      p.gradeList = gradeList(p.grades);
      p.pin = pinLevel(p.gradeList);
      p.haystack = " " + norm([p.school, p.campus, p.also_known_as, p.address, p.doe_code].join(" "));
      codes[p.doe_code] = 1;
      return p;
    });
    totalSchools = Object.keys(codes).length;
    buildControls();
    addLegend();
    render({ fit: true });
  }).catch(function (err) {
    $("summary").textContent = "The school data didn't load. Refresh the page to try again.";
    console.error(err);
  });
})();
