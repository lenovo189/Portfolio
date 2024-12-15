// Initialize Particle.js
particlesJS.load('particles-js', 'particles.json', function() {
    console.log('callback - particles.js config loaded');
});

// GSAP Animation Example
gsap.from("header", { duration: 1, y: -100, opacity: 0 });
gsap.from("section", { duration: 1, opacity: 0, stagger: 0.3 });