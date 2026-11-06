/* NYC charter school campus map. Data: data/campuses.geojson, built by scripts/build_data.py */
(function () {
  "use strict";

  var LEVELS = {
    elem:    { label: "Elementary" },
    elemmid: { label: "Elementary + Middle" },
    mid:     { label: "Middle" },
    high:    { label: "High (incl. 6-12)" },
    k12:     { label: "K-12" }
  };

  function levelOf(levels) {
    var e = levels.indexOf("Elementary") >= 0, m = levels.indexOf("Middle") >= 0, h = levels.indexOf("High") >= 0;
    if (e && h) return "k12";
    if (h) return "high";
    if (e && m) return "elemmid";
    if (m) return "mid";
    return "elem";
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  var map = L.map("map", { zoomControl: true, preferCanvas: false });
  // OpenStreetMap tiles (free with attribution), softened in CSS so the coloured pins stand out.
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    className: "basemap",
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  }).addTo(map);
  map.setView([40.70, -73.94], 11);

  function campusHtml(p, showAddress) {
    var gradesNote = p.grades_2025_26
      ? ' <span class="note">(as of 2025-26; school is ' + esc(p.school_grades) + " in 2026-27)</span>"
      : (p.school_grades && p.school_grades !== p.grades && p.school_campuses > 1
        ? ' <span class="note">(school: ' + esc(p.school_grades) + ")</span>" : "");
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
      "<dt>Grades</dt><dd>" + esc(p.grades) + gradesNote + "</dd>" +
      "<dt>District</dt><dd>" + esc(p.district) + " · " + esc(p.borough) + "</dd>" +
      "<dt>Authorizer</dt><dd>" + esc(p.authorizer) + "</dd>" +
      "<dt>Opened</dt><dd>" + esc(p.opened) + "</dd>" +
      (p.school_campuses > 1 ? "<dt>Campuses</dt><dd>" + p.school_campuses + " (this is one)</dd>" : "") +
      "</dl>" +
      (p.approximate ? '<p class="note">Location approximate: this address is not in the city address database, so the pin is at the nearest address on the same street.</p>' : "") +
      '<div class="links">' + links.join("") + "</div>" +
      "</div>";
  }

  function buildingHtml(feats) {
    var shared = feats.length > 1;
    var head = shared
      ? '<p class="building">' + feats.length + " schools share this building</p>" +
        '<p class="address">' + esc(feats[0].properties.address) + "</p>" : "";
    return '<div class="popup">' + head + feats.map(function (f) { return campusHtml(f.properties, !shared); }).join("") + "</div>";
  }

  function iconFor(feats) {
    if (feats.length > 1) {
      return L.divIcon({ className: "", iconSize: [22, 22], iconAnchor: [11, 11], popupAnchor: [0, -10],
        html: '<div class="pin shared lvl-shared">' + feats.length + "</div>" });
    }
    var lv = levelOf(feats[0].properties.levels);
    return L.divIcon({ className: "", iconSize: [14, 14], iconAnchor: [7, 7], popupAnchor: [0, -6],
      html: '<div class="pin lvl-' + lv + '"></div>' });
  }

  function addLegend() {
    var ctl = L.control({ position: "bottomright" });
    ctl.onAdd = function () {
      var div = L.DomUtil.create("div", "legend");
      var rows = Object.keys(LEVELS).map(function (k) {
        return '<div class="row"><span class="pin lvl-' + k + '"></span>' + LEVELS[k].label + "</div>";
      }).join("") + '<div class="row"><span class="pin shared lvl-shared">3</span>Shared building (number of schools)</div>' +
        '<div class="row"><span class="cluster legend-cluster">12</span>Nearby campuses (zoom in)</div>';
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

  fetch("data/campuses.geojson")
    .then(function (r) { return r.json(); })
    .then(function (gj) {
      var feats = gj.features;
      // Group campuses by building so every school in a shared building is reachable from one pin.
      var buildings = {};
      feats.forEach(function (f) {
        var c = f.geometry.coordinates;
        var key = f.properties.bin || (c[1].toFixed(5) + "," + c[0].toFixed(5));
        (buildings[key] = buildings[key] || []).push(f);
      });
      // Nearby pins merge into a count bubble at low zoom and split apart as you zoom in.
      var layer = L.markerClusterGroup({
        maxClusterRadius: 32,
        disableClusteringAtZoom: 15,
        spiderfyOnMaxZoom: true,
        showCoverageOnHover: false,
        iconCreateFunction: function (cluster) {
          var n = 0;
          cluster.getAllChildMarkers().forEach(function (m) { n += m.options.campuses; });
          var size = n < 10 ? 30 : n < 30 ? 36 : 42;
          return L.divIcon({ className: "", iconSize: [size, size],
            html: '<div class="cluster" style="width:' + size + "px;height:" + size + 'px" aria-label="' + n +
                  ' campuses, zoom in to see them">' + n + "</div>" });
        }
      });
      var byBorough = {};
      Object.keys(buildings).forEach(function (key) {
        var group = buildings[key].sort(function (a, b) { return a.properties.school.localeCompare(b.properties.school); });
        var c = group[0].geometry.coordinates;
        var title = group.length > 1 ? group.length + " schools: " + group.map(function (f) { return f.properties.school; }).join("; ")
                                     : group[0].properties.school;
        var m = L.marker([c[1], c[0]], { icon: iconFor(group), title: title, alt: title, riseOnHover: true, campuses: group.length })
          .bindPopup(buildingHtml(group), { maxWidth: 320, autoPanPaddingTopLeft: [56, 20], autoPanPaddingBottomRight: [20, 20] });
        layer.addLayer(m);
        var b = group[0].properties.borough;
        (byBorough[b] = byBorough[b] || []).push([c[1], c[0]]);
      });
      layer.addTo(map);
      var all = layer.getBounds();
      map.fitBounds(all, { padding: [20, 20] });

      var schools = {};
      feats.forEach(function (f) { schools[f.properties.doe_code] = 1; });
      document.getElementById("summary").textContent =
        Object.keys(schools).length + " schools · " + feats.length + " campuses · " + Object.keys(buildings).length + " buildings";

      var buttons = document.querySelectorAll(".boroughs button");
      buttons.forEach(function (btn) {
        btn.addEventListener("click", function () {
          var b = btn.getAttribute("data-borough");
          buttons.forEach(function (x) { x.setAttribute("aria-pressed", x === btn ? "true" : "false"); });
          map.closePopup();
          if (b === "all") map.fitBounds(all, { padding: [20, 20] });
          else map.fitBounds(L.latLngBounds(byBorough[b]), { padding: [30, 30] });
        });
      });
      addLegend();
    })
    .catch(function (err) {
      document.getElementById("summary").textContent = "Could not load the school data.";
      console.error(err);
    });
})();
