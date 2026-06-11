(function attachWorkspaceCamera(global) {
  const { OrbitControls, THREE } = global;

  function create(camera, domElement) {
    if (!OrbitControls) {
      throw new Error('OrbitControls failed to load.');
    }

    const controls = new OrbitControls(camera, domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.maxPolarAngle = Math.PI * 0.49;
    controls.minDistance = 18;
    controls.maxDistance = 2600;
    return controls;
  }

  function createKeyboardPanState() {
    return {
      ArrowDown: false,
      ArrowLeft: false,
      ArrowRight: false,
      ArrowUp: false,
    };
  }

  function bindKeyboardPan(state) {
    function onKeyDown(event) {
      if (!(event.key in state)) {
        return;
      }

      const target = event.target;
      const tagName = target && target.tagName ? target.tagName.toLowerCase() : '';
      const isTyping =
        (target && target.isContentEditable) ||
        tagName === 'input' ||
        tagName === 'textarea' ||
        tagName === 'select';

      if (isTyping) {
        return;
      }

      state[event.key] = true;
      event.preventDefault();
    }

    function onKeyUp(event) {
      if (!(event.key in state)) {
        return;
      }

      state[event.key] = false;
      event.preventDefault();
    }

    function onBlur() {
      Object.keys(state).forEach((key) => {
        state[key] = false;
      });
    }

    window.addEventListener('keydown', onKeyDown);
    window.addEventListener('keyup', onKeyUp);
    window.addEventListener('blur', onBlur);

    return () => {
      window.removeEventListener('keydown', onKeyDown);
      window.removeEventListener('keyup', onKeyUp);
      window.removeEventListener('blur', onBlur);
    };
  }

  function applyKeyboardPan(camera, controls, state, deltaSeconds) {
    let moveForward = 0;
    let moveRight = 0;

    if (state.ArrowUp) {
      moveForward += 1;
    }
    if (state.ArrowDown) {
      moveForward -= 1;
    }
    if (state.ArrowRight) {
      moveRight += 1;
    }
    if (state.ArrowLeft) {
      moveRight -= 1;
    }

    const magnitude = Math.hypot(moveForward, moveRight);
    if (magnitude < 1e-8) {
      return;
    }

    moveForward /= magnitude;
    moveRight /= magnitude;

    const forward = new THREE.Vector3().subVectors(controls.target, camera.position);
    forward.y = 0;

    if (forward.lengthSq() < 1e-8) {
      forward.set(1, 0, 0);
    }

    forward.normalize();
    const right = new THREE.Vector3(-forward.z, 0, forward.x).normalize();
    const radius = camera.position.distanceTo(controls.target);
    const panMetersPerSecond = Math.max(8, radius * 0.55);
    const step = panMetersPerSecond * deltaSeconds;

    controls.target.addScaledVector(right, moveRight * step);
    controls.target.addScaledVector(forward, moveForward * step);
    camera.position.addScaledVector(right, moveRight * step);
    camera.position.addScaledVector(forward, moveForward * step);
    controls.update();
  }

  function frameGrid(camera, controls, grid) {
    const width = Math.max(1, grid.maxX - grid.minX);
    const height = Math.max(1, grid.maxY - grid.minY);
    const relief = Math.max(10, grid.maxElevation - grid.minElevation);
    const span = Math.max(width, height);
    const distance = Math.max(span * 0.95, relief * 4.5, 80);

    controls.target.set(0, relief * 0.2, 0);
    camera.position.set(distance * 0.72, relief + distance * 0.5, distance * 0.72);
    camera.near = 0.1;
    camera.far = Math.max(10000, distance * 20);
    camera.updateProjectionMatrix();
    controls.update();
  }

  global.VEILCamera = {
    applyKeyboardPan,
    bindKeyboardPan,
    create,
    createKeyboardPanState,
    frameGrid,
  };
})(window);
