import React from "react";

export default function Navbar() {
  const navStyle = {
    width: "100%",
    padding: "1rem 1.5rem",
    display: "flex",
    justifyContent: "center",
    gap: "2rem",
    borderBottom: "1px solid rgba(255,255,255,0.1)",
    backgroundColor: "rgba(13, 15, 20, 0.9)",
    position: "sticky",
    top: 0,
    zIndex: 50,
  };

  const linkStyle = {
    color: "#C5C8CE",
    fontWeight: 500,
    textDecoration: "none",
  };

  const hover = (e, color) => (e.target.style.color = color);

  return (
    <header style={navStyle}>
      <a href="/" style={{ ...linkStyle, color: "#2D9CFF", fontWeight: "bold" }}>ReneAI</a>
      <a href="/features" style={linkStyle} onMouseEnter={(e)=>hover(e,"#2D9CFF")} onMouseLeave={(e)=>hover(e,"#C5C8CE")}>Features</a>
      <a href="/industries" style={linkStyle} onMouseEnter={(e)=>hover(e,"#2D9CFF")} onMouseLeave={(e)=>hover(e,"#C5C8CE")}>Industries</a>
      <a href="/contact" style={linkStyle} onMouseEnter={(e)=>hover(e,"#2D9CFF")} onMouseLeave={(e)=>hover(e,"#C5C8CE")}>Contact</a>
      <a href="/about" style={linkStyle} onMouseEnter={(e)=>hover(e,"#2D9CFF")} onMouseLeave={(e)=>hover(e,"#C5C8CE")}>About</a>
    </header>
  );
}
