from typing import List, Dict, Tuple, Set

from webdnn.backend.code_generator.allocator import MemoryLayout
from webdnn.backend.code_generator.injectors.kernel_name_injector import KernelNameInjector
from webdnn.backend.webgl.generator import WebGLDescriptorGenerator
from webdnn.backend.webgl.kernel import Kernel
from webdnn.backend.webgl.uniform_injector import UniformInjector
from webdnn.graph.axis import AxisKeyDict, Axis
from webdnn.graph.operators.elementwise_add import ElementwiseAdd
from webdnn.graph.order import Order
from webdnn.graph.variable import Variable
from webdnn.util.misc import mul


def _simplify_orders(variables: List[Variable]) -> Tuple[Dict[Variable, Order], Dict[Variable, AxisKeyDict[int]]]:
    """
    Simplify variable orders based on follow rules

    - Axis whose size is :code:`1` will be removed.

    - If axis :code:`A` and :code:`B` is adjacent in all variables which has axis :code:`A` and axis :code:`B`, :code:`A` and :code:`B` will
      be merged.
        - For example, :code:`OrderABC` and :code:`OrderCAB` can be simplified as :code:`OrderXC` and :code:`OrderCX`
        - In this case, the size of axis :code:`X` is calculated as :code:`(size of axis A) * (size of axis B)`

    ...code-block::text

        ex)
            x0.order=NHWC,  simplify    x0.order=X
             y.order=NHWC ------------>  y.order=X

        ex)
            x0.order=C,     simplify    x0.order=C
            x1.order=NHWC ------------> x1.order=XC
             y.order=NHWC                y.order=XC

        ex)
            x0.order=C,     simplify    x0.order=C
            x1.order=HW   ------------> x1.order=X
             y.order=NHWC                y.order=NXC

    Returns:
        (tuple of dicts) simplified orders and shape
    """

    orders = {}  # type: Dict[Variable, Order]
    shape_dicts = {}  # type: Dict[Variable, AxisKeyDict[int]]
    axis_scalar = Axis("Scalar")

    # remove all axes whose size is `1`.
    for v in variables:
        new_axes = [a for a in v.order.axes if v.shape_dict[a] != 1]
        orders[v] = Order(new_axes)
        shape_dicts[v] = AxisKeyDict(new_axes, [v.shape_dict[a] for a in new_axes])

        if len(new_axes) == 0 and v.size == 1:
            orders[v] = Order([axis_scalar])
            shape_dicts[v] = {axis_scalar: 1}

    # list up all axes and variables which have the axis
    var_dict = AxisKeyDict[Set[Variable]]()
    for v in variables:
        for axis in orders[v].axes:
            if axis in var_dict:
                var_dict[axis].add(v)
            else:
                var_dict[axis] = {v}

    # find pair of axes which can be merged
    counter = 0
    flag_continue = True
    while flag_continue:
        flag_continue = False

        for axis1, vars1 in list(var_dict.items()):
            for axis2, vars2 in list(var_dict.items()):
                if axis1 == axis2:
                    continue

                if vars1 != vars2 or any(orders[v].axes_dict[axis1] + 1 != orders[v].axes_dict[axis2] for v in vars1):
                    # `axis1` and `axis2` must be adjacent.
                    continue

                # merge `axis1` and `axis2` into `axis_new`

                axis_new = Axis(f"X{counter}")
                counter += 1

                for v in vars1:
                    shape_dict = shape_dicts[v]
                    shape_dict[axis_new] = shape_dict[axis1] * shape_dict[axis2]
                    del shape_dict[axis1]
                    del shape_dict[axis2]

                    order = orders[v]
                    orders[v] = Order(order.axes[:order.axes_dict[axis1]] + (axis_new,) + order.axes[order.axes_dict[axis2] + 1:])

                var_dict[axis_new] = vars1
                del var_dict[axis1]
                del var_dict[axis2]

                flag_continue = True
                break

            if flag_continue:
                break

    return orders, shape_dicts


def _optimize_loop_structure(variables: List[Variable]):
    """
    Optimize loop structure to iterate each element in variables

    Returns:
        (tuple): three elements are returned

        - First element is dictionary of orders with key of each variable and value of each variable's order. This order is
        simplified to avoid unnecessary deep loop.
        - Second element is shape dictionary of all variables.
        - The last element is stride dictionary of all variables.
    """
    orders, shape_dicts = _simplify_orders(variables)
    shapes = {v: [shape_dicts[v][a] for a in orders[v].axes] for v in variables}
    strides = {v: [mul(shapes[v][i + 1:]) for i in range(v.ndim)] for v in variables}
    stride_dicts = {v: AxisKeyDict(orders[v].axes, strides[v]) for v in variables}

    # re-ordering
    axes = []
    for v in sorted(variables, key=lambda v: orders[v].ndim):
        axes += [axis for axis in orders[v].axes if axis not in axes]

    orders = {v: Order(list(filter(lambda x: x in orders[v].axes, axes))) for v in variables}

    return orders, shape_dicts, stride_dicts


template = """
precision mediump float;

%%UNIFORM(float, W)%%;
%%UNIFORM(float, H)%%;
%%UNIFORM(sampler2D, X0)%%;
%%UNIFORM(sampler2D, X1)%%;

void main() {
    int x = int(gl_FragCoord.x - 0.5);
    int y = int(gl_FragCoord.y - 0.5);
    int index = y * int(W) + x; 
    
    vec2 pos0 = vec2(float(x) / W, float(y) / H);
    vec2 pos1 = vec2(float(x) / W, float(y) / H);
    
    float v0 = texture2D(X0, pos0).r;
    float v1 = texture2D(X1, pos1).r;
    
    gl_FragColor = vec4(v0 + v1, 0, 0, 0);
}
"""


@WebGLDescriptorGenerator.register_handler(ElementwiseAdd)
def elementwise_add(op: ElementwiseAdd, memory_layout: MemoryLayout) -> List[Kernel]:
    x0 = memory_layout[op.inputs["x0"]]
    x1 = memory_layout[op.inputs["x1"]]
    y = memory_layout[op.outputs["y"]]

    orders, shape_dicts, stride_dicts = _optimize_loop_structure(list(op.inputs.values()) + list(op.outputs.values()))
    print(orders, shape_dicts, stride_dicts)

    name_injector = KernelNameInjector(op)
    uniform_injector = UniformInjector()
    uniform_injector.register({
        "X0": x0.variable,
        "X1": x1.variable,
        "W": y.size if y.size < 1024 else 1024,
        "H": (y.size + 1024 - 1) // 1024
    })

    source = template
    source = name_injector.inject(source)
    source = uniform_injector.inject(source)

    kernel = Kernel(
        source,
        name_injector.name,
        uniform_injector.samplers,
        uniform_injector.uniforms,
        y.variable
    )

    return [kernel]
